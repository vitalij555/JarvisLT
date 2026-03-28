# JarvisLT Architecture

This document describes the design of JarvisLT — the component breakdown, data flows, extension points, and rationale behind key decisions.

---

## Design Philosophy

1. **Local-first.** All inference (STT, TTS, wake word, embeddings) runs on-device. Only LLM calls and MCP servers reach the internet. Neo4j and ChromaDB run in Docker or embedded — no cloud DB accounts.

2. **Config-driven extensibility.** New MCP servers, scheduled tasks, and voices are added in `config.yaml`. No code changes needed for the common case.

3. **LLM orchestration over hard-coded logic.** Rather than writing scraping or summarisation code per data source, we give the LLM tools (web_crawl, memory_recall, etc.) and let it compose them. This generalises to arbitrary sources without new code.

4. **Additive architecture.** Every new capability registers tools into the existing loop via `LLMClient.register_local_tools()`. The core assistant loop never changes; new packages plug in at the edges.

5. **Graceful degradation.** Neo4j unavailable → memory tools return "unavailable" without crashing. ChromaDB fails → semantic search disabled but entity search still works. crawl4ai not installed → web_crawl returns an install hint.

---

## Async Runtime

The entire system runs on a single `asyncio` event loop (`asyncio.run(assistant.run())`). Key concurrency decisions:

- **OpenAI API** (synchronous SDK) is called via `loop.run_in_executor(None, ...)` to avoid blocking the event loop.
- **ChromaDB** (synchronous) is wrapped the same way in `VectorStore`.
- **Home Assistant connector** (synchronous) is dispatched via `run_in_executor` in `_handle_local_tool`.
- **Async handlers** (memory tools, task tools, web tools) are awaited directly — detected via `asyncio.iscoroutinefunction`.
- **Background summarisation** runs as `asyncio.create_task(...)` after each assistant turn — never blocks the wake-word loop.
- **APScheduler** uses `AsyncIOScheduler` — shares the event loop, no threads or subprocesses needed.

---

## Main Loop

```
asyncio.run(assistant.run())
  │
  ├── await llm.start()              # launch MCP subprocesses, discover tools
  ├── await memory_manager.init()    # connect Neo4j, init ChromaDB
  ├── await task_runner.start()      # load + schedule all tasks
  │
  └── loop forever:
        await wake_detector.wait_for_wake_word()   # blocks on Wyoming TCP
        await speaker.speak("Yes?")
        text = await listener.listen()             # VAD → STT via Wyoming TCP
        memory.add_turn("user", text)
        response = await llm.chat_async(text, memory)   # tool-use loop (see below)
        memory.add_turn("assistant", response)
        memory.save()
        asyncio.create_task(memory_manager.summarize_and_store(...))   # background
        await speaker.speak(response)
```

---

## LLM Tool-Use Loop

`LLMClient.chat_async()` implements the standard tool-use pattern:

```
Build messages = [system, ...context, user_text]
loop:
  response = OpenAI.chat.completions.create(messages, tools)
  if finish_reason == "tool_calls":
    for each tool_call:
      if tool in _mcp_tool_map  → await session.call_tool()       (MCP)
      else                      → await _handle_local_tool()       (local)
    append tool results to messages
    continue
  else:
    return response text
```

**Tool registration** — `register_local_tools(schemas, handler)` adds tool schemas to `_local_tools` and maps each tool name to its handler in `_local_tool_map`. Called in `assistant.__init__()` for each capability package:

```python
llm.register_local_tools(HA_TOOLS,     ha_connector)        # sync handler
llm.register_local_tools(MEMORY_TOOLS, MemoryToolHandler(…)) # async handler
llm.register_local_tools(TASK_TOOLS,   TaskToolHandler(…))   # async handler
llm.register_local_tools(WEB_TOOLS,    WebToolHandler())     # async handler
```

Sync vs async is detected automatically via `asyncio.iscoroutinefunction`. Adding a new capability never touches the tool-use loop — only `assistant.__init__()` and the new package.

---

## Memory Architecture

Two complementary stores handle different access patterns:

### Neo4j — Structured Entity Memory

Nodes and relationships for things that have identity:

```
(:Entity {id, name, type, notes})
  -[:HAS_FACT {key, value, source}]-> (:Fact)
  -[:RELATED_TO {label}]-> (:Entity)
```

Entity types: `person`, `place`, `event`, `preference`, `task`, `other`

**Access pattern:** exact lookup by name, type listing, relationship traversal. Answers "What do I know about Sofia?" with structured facts.

**Why Neo4j over SQLite:** User explicitly chose it. For a personal assistant at this scale (hundreds to low thousands of nodes) either works, but Neo4j gives a real graph query language (Cypher) and a browser UI at `localhost:7474` for inspecting/editing memory. Migration path from SQLite to Neo4j would have been straightforward if the choice had gone the other way — the `EntityStore` class abstracts the backend.

### ChromaDB — Semantic Vector Memory

Two collections with local embeddings (`all-MiniLM-L6-v2`, computed offline):

- `conversation_summaries` — LLM-generated bullet-point summaries of past sessions
- `task_results` — output text from every scheduled task run

**Access pattern:** fuzzy semantic search. Answers "What did we say about the dentist?" even if the word "dentist" never appeared in an entity name.

**Summarisation strategy:** After each assistant turn, `summarize_and_store()` runs as a background task. It sends the last 20 turns to the LLM asking for a 3-5 bullet summary, then stores the result. Running per-turn rather than per-session means no "session boundary" logic is needed.

### Hybrid Recall

`memory_manager.recall_about(topic)` runs both stores in parallel:
1. Neo4j entity search → structured facts (high precision)
2. ChromaDB vector search → fuzzy past-conversation matches (high recall)

Results are merged into a single formatted string returned to the LLM as the tool result.

---

## Scheduler Architecture

### Static tasks (config.yaml)

Defined under `scheduled_tasks:` with a cron expression or `interval_minutes`. Loaded at startup. Restart required to change.

### Dynamic tasks (voice-created)

`task_create` LLM tool writes to `task_definitions` table in `jarvis_tasks.db` with `source="voice"`. `TaskRunner.start()` loads both config tasks and DB tasks, so voice-created tasks survive restarts.

### HeadlessSession

The key abstraction for running LLM without audio:

```python
class HeadlessSession:
    async def run(self, prompt: str) -> str:
        memory = ConversationMemory(max_turns=10, persist_path=None)
        return await self._llm.chat_async(prompt, memory)
```

It reuses the already-started `LLMClient` (MCP servers already running), uses a fresh empty memory per task (no bleed between tasks), and has access to all registered tools (memory, web_crawl, HA, MCP). A scheduled task can call `web_crawl`, then `memory_remember`, then check Gmail — all in one prompt, using the full tool stack.

### Task delivery

Only **store** mode is implemented. Results go to:
1. `task_runs` table in `jarvis_tasks.db` (queryable via `task_get_recent_results`)
2. `task_results` ChromaDB collection (queryable via `memory_search_history`)

The user retrieves results interactively: *"What happened while I slept?"* → `task_get_recent_results(hours=8)`.

---

## Web Crawling Architecture

Two Playwright-backed tools with different trade-offs:

### `web_crawl` (local tool — crawl4ai)

Best for scheduled tasks and bulk content extraction:

```
web_crawl(url, max_depth=1, max_pages=10, topic_filter=None)
  → crawl4ai AsyncWebCrawler
    ├── max_depth=1: single arun(url)
    └── max_depth>1: BFSDeepCrawlStrategy(max_depth, max_pages)
  → list[CrawlResult] → formatted markdown → LLM
```

- Fetches the listing page, extracts all internal links, fetches each one
- Returns clean markdown (crawl4ai strips navigation, ads, boilerplate)
- Optional `topic_filter` keyword pre-filter (keeps only relevant paragraphs)
- `_PAGE_CHAR_LIMIT = 8000` per page, `_TOTAL_CHAR_LIMIT = 40000` total — prevents context flooding

### `@playwright/mcp` (MCP server — Microsoft)

Best for interactive tasks requiring navigation:

- The LLM gets `navigate`, `snapshot`, `click`, `fill`, `type` and ~20 other browser tools
- Useful when content requires clicking "load more", filling a search form, or navigating pagination
- The LLM drives the browser autonomously, step by step

**Why both?** `web_crawl` is one tool call per batch — efficient, deterministic, good for scheduled tasks. `@playwright/mcp` gives fine-grained control but requires the LLM to orchestrate each browser action — better for interactive/one-off browsing.

---

## Search & Places Architecture

### `google_search` (local tool — Serper.dev)

Single async `httpx` POST to `https://google.serper.dev/search`. Returns:
- Answer box / featured snippet (direct answers for factual queries)
- Knowledge graph card (for people, places, organisations)
- Organic results: title, URL, snippet

Requires `SERPER_API_KEY` env var. 2500 free searches/month. No API key → returns install hint, does not crash.

**Why Serper over alternatives:**
- Google Custom Search API: killed "Search the entire web" for new engines as of Jan 2026
- Brave Search API: no free tier
- DuckDuckGo scraping: unofficial, fragile
- Serper: real Google results, generous free tier, simple REST

### `search_places` (local tool — Google Places API)

Two-phase async call using `httpx`:

```
search_places(query, location, radius_meters, open_now, max_results)
  → geocode(location)        # GET /maps/api/geocode/json → lat,lng
  → nearby_search(coords)    # GET /maps/api/place/nearbysearch/json
  OR (if geocode fails)
  → text_search(query+loc)   # GET /maps/api/place/textsearch/json
  → formatted results (name, address, rating, price level, open status)
```

Geocoding first gives precise radius control. Text Search fallback handles vague location strings ("Vilnius old town") that don't geocode cleanly.

**Nearby queries** work best when the user's home address is stored in Neo4j memory:
*"Find coffee shops near me"* → LLM calls `memory_recall("home address")` → passes result to `search_places(location=<address>)`.

Requires `GOOGLE_PLACES_API_KEY` env var (simple API key, not OAuth). No key → returns install hint.

---

## Extension Points

### Adding a new capability

1. Create `jarvis/connectors/my_thing.py` with `MY_TOOLS` (list of tool schema dicts) and `MyToolHandler` (class with async `handle_tool_call`)
2. In `jarvis/core/assistant.py` `__init__`:
   ```python
   self.llm.register_local_tools(MY_TOOLS, MyToolHandler(...))
   ```
3. Done. The tool appears in the LLM's context on next startup.

### Adding an MCP server

In `config.yaml`:
```yaml
mcp_servers:
  my_server:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-name"]
```

No code changes. Tools discovered automatically via `session.list_tools()`.

### Adding a scheduled task (via voice)

*"Jarvis, create a task called weekly_mastercard that runs every Monday at 9am: use web_crawl on https://www.mastercard.com/us/en/news-and-trends/stories.html with max_depth=2 and summarise the news."*

The `task_create` tool writes to `jarvis_tasks.db` and schedules it live in APScheduler.

---

## Technology Decisions

| Decision | Choice | Alternatives considered | Reason |
|---|---|---|---|
| LLM | OpenAI gpt-4o | — | User requirement — not changed |
| Graph memory | Neo4j | SQLite 3-table schema | User preference; real Cypher; browser UI at :7474 |
| Vector memory | ChromaDB embedded | Qdrant, pgvector, sqlite-vec | Zero extra server, offline embeddings bundled, single pip install |
| Scheduler | APScheduler AsyncIOScheduler | Celery+Redis, system cron | In-process, shares event loop, SQLite job store, no infra |
| Task persistence | aiosqlite | — | Lightweight, async, shares existing Python runtime |
| Web scraping | crawl4ai + Playwright | BeautifulSoup, Scrapy, Selenium | LLM-friendly markdown output, JS rendering, depth crawling, no DOM code |
| Interactive browsing | @playwright/mcp | Puppeteer MCP | Official Microsoft release, ~26 tools, headless flag |
| Web search | Serper.dev REST | Google CSE (killed whole-web in 2026), Brave (no free tier), DDG scraping | Real Google results, 2500 free/month, simple REST, no infra |
| Places search | Google Places API | Foursquare, HERE | Real Google data, geocoding + nearby search, same API key model as other Google services |
| STT | wyoming-faster-whisper | Whisper API, Vosk | Local, offline, Wyoming protocol shared with wake word |
| TTS | speaches (Kokoro-82M) | Piper, Coqui | OpenAI-compatible REST, high quality voice, local |
| Wake word | wyoming-openwakeword | Porcupine, Snowboy | Free, local, Wyoming protocol |

---

## Known Limitations

- **Neo4j startup latency:** If Neo4j is slow to start, entity tools log a warning and return "unavailable" rather than crashing. Retry not implemented — restart assistant after Neo4j is ready.
- **crawl4ai deep_crawling import:** `BFSDeepCrawlStrategy` is in `crawl4ai.deep_crawling` which may not exist in older versions. `WebCrawler` falls back to single-page fetch gracefully.
- **Conversation summarisation frequency:** `summarize_and_store` runs after every single assistant turn, which makes an additional LLM API call per turn. For high-frequency use this could be throttled to run only every N turns or on a time-based schedule.
- **No speaker interruption:** If a scheduled task completes while the assistant is speaking, the result is only stored — it will not interrupt or queue after the current speech.
- **Context window for long crawls:** `_TOTAL_CHAR_LIMIT = 40_000` characters from web crawls are returned to the LLM as a tool result. Very long crawls are truncated. The LLM must summarise within its context window.

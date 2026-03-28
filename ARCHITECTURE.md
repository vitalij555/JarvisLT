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
- **TTS playback** (`sd.play` + `sd.wait`) is dispatched via `run_in_executor` so the event loop is not blocked during audio output.

---

## Main Loop

```
asyncio.run(assistant.run())
  │
  ├── await llm.start()              # launch MCP subprocesses, discover tools
  ├── await memory_manager.init()    # connect Neo4j, init ChromaDB
  ├── await task_runner.start()      # load + schedule all tasks
  │
  └── outer loop (wake word):
        await wake_detector.wait_for_wake_word()
        await speaker.speak("Yes?")

        inner loop (conversation mode):
          text = await listener.listen(timeout=current_timeout)
          if no speech within timeout:
            if in_conversation: await speaker.speak("Going to sleep...")
            break  →  back to outer loop

          memory.add_turn("user", text)
          response = await llm.chat_async(text, memory)   # tool-use loop (see below)
          memory.add_turn("assistant", response)
          memory.save()
          asyncio.create_task(memory_manager.summarize_and_store(...))   # background
          await speaker.speak(response)
          current_timeout = conversation_timeout  # 60s for follow-ups
```

The first listen after "Yes?" uses `speech_timeout` (8s default). After each response, the listener waits up to `conversation_timeout` (60s) for a follow-up before announcing sleep and returning to wake-word detection.

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

**Important:** Tool call results (including fetched email content, IDs, etc.) exist only within the `messages` list of a single `chat_async` call. They are NOT persisted to `ConversationMemory`. Only the final text response is stored. On follow-up turns, the LLM must re-fetch items using identifiers (subject, sender, date) mentioned in the prior text response.

---

## Multilingual Architecture

### Speech-to-Text

wyoming-faster-whisper runs with `--model small-int8 --language auto`. Whisper detects the spoken language automatically — Lithuanian, Polish, Russian, and mixed-language input (e.g. English sentence with a Lithuanian proper noun) are all transcribed correctly.

### Text-to-Speech

`Speaker` routes each utterance through a two-stage decision:

```
speak(text)
  → _detect_language(text)
      ├── text < 60 chars or >30% ASCII words  →  "en"  (short/mixed content)
      └── langdetect.detect(text)              →  ISO 639-1 code
  → lang in multilingual_voices?
      ├── yes  →  _speak_edge_tts(text, voice)  →  miniaudio decode  →  sd.play
      └── no   →  _speak_speaches(text)         →  WAV response      →  sd.play
```

The 30%-ASCII-word heuristic prevents mixed responses like *"Found it! Subject: 'Penktadienio laiškas'"* from being classified as Lithuanian — the English framing words keep it above the threshold.

**Voice mapping** (configurable in `config.yaml` under `speaches.multilingual_voices`):

| Language | Engine | Default voice |
|---|---|---|
| English (and default) | speaches / Kokoro-82M | `af_heart` |
| Lithuanian (`lt`) | edge-tts | `lt-LT-OnaNeural` |
| Polish (`pl`) | edge-tts | `pl-PL-ZofiaNeural` |
| Russian (`ru`) | edge-tts | `ru-RU-SvetlanaNeural` |

Any language not in the mapping falls back to speaches/English. Additional languages are added by extending `multilingual_voices` — no code changes needed.

---

## Voice Activity Detection (VAD)

`SpeechListener.listen()` implements a simple energy-based VAD:

```
Open mic stream
deadline = now + speech_timeout   (8s default)

loop:
  chunk = mic_queue.get(timeout=0.2)
  if Empty and not got_speech and now > deadline:
    return ""    ← timeout, no speech detected

  rms = sqrt(mean(chunk²))
  send chunk to Wyoming STT

  if rms > silence_threshold:
    got_speech = True; reset silent_chunks
  elif got_speech:
    silent_chunks += 1
    if silent_chunks >= silence_chunks:
      break    ← speech ended (silence_duration of silence)

Send AudioStop → read Transcript → return text
```

Key parameters (all tunable in `config.yaml`):

| Parameter | Default | Effect |
|---|---|---|
| `silence_threshold` | 100 | RMS below this = silence. Lower = more sensitive to quiet voices. |
| `silence_duration` | 1.5s | Trailing silence before cutting off recording |
| `speech_timeout` | 8.0s | Max wait for first speech before giving up (prevents infinite hang) |
| `conversation_timeout` | 60.0s | Max wait for follow-up; passed as `timeout` override to `listen()` |

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

**Why Neo4j over SQLite:** User explicitly chose it. For a personal assistant at this scale (hundreds to low thousands of nodes) either works, but Neo4j gives a real graph query language (Cypher) and a browser UI at `localhost:7474` for inspecting/editing memory.

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

## Email Search Strategy

The system prompt instructs the LLM to follow a two-step strategy when searching for emails by organisation name:

1. **Keyword search first** — query Gmail with the org name as plain text (e.g. `query="KMM school"`). This avoids guessing domains and works for any sender whose name or domain contains the keyword.
2. **Web search fallback** — if keyword search fails, call `google_search` to find the org's official email domain, then re-query Gmail with `from:<domain>`.

On multi-turn follow-ups (e.g. "summarize it"), the LLM re-queries Gmail by subject and sender from the prior turn's text response. **Email IDs are never reused across turns** — they exist only within a single `chat_async` call and are not persisted.

---

## Scheduler Architecture

### Static tasks (config.yaml)

Defined under `scheduled_tasks:` with a cron expression or `interval_minutes`. Loaded at startup. Restart required to change. An empty or commented-out `scheduled_tasks:` section (parsed as `None` by YAML) is handled gracefully.

### Dynamic tasks (voice-created)

`task_create` LLM tool writes to `task_definitions` table in `jarvis_tasks.db` with `source="voice"`. `TaskRunner.start()` loads both config tasks and DB tasks, so voice-created tasks survive restarts.

### Task recording from conversation

The system prompt instructs the LLM to handle *"record this as a task"* / *"save last command as task named X"* meta-commands by:
1. Looking at the most recent action in conversation history
2. Reconstructing a fully self-contained prompt (no references to "what you just did")
3. Asking for a schedule if not given
4. Calling `task_create`

### HeadlessSession

The key abstraction for running LLM without audio:

```python
class HeadlessSession:
    async def run(self, prompt: str) -> str:
        memory = ConversationMemory(max_turns=10, persist_path=None)
        return await self._llm.chat_async(prompt, memory)
```

It reuses the already-started `LLMClient` (MCP servers already running), uses a fresh empty memory per task (no bleed between tasks), and has access to all registered tools (memory, web_crawl, HA, MCP).

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

### Adding a language

In `config.yaml` under `speaches.multilingual_voices`, add the ISO 639-1 code and an edge-tts voice name:
```yaml
multilingual_voices:
  de: "de-DE-KatjaNeural"
```

No code changes. `langdetect` will classify the text; `Speaker` will route it.

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
| STT | wyoming-faster-whisper small-int8, auto language | Whisper API, Vosk | Local, offline, multilingual, Wyoming protocol shared with wake word |
| TTS (English) | speaches / Kokoro-82M | Piper, Coqui | OpenAI-compatible REST, high quality voice, local |
| TTS (other languages) | edge-tts | gTTS, Azure TTS | Free, neural quality, no API key, supports Lithuanian/Polish/Russian, no ffmpeg needed |
| Language detection | langdetect | langid, fastText | Simple pip install, offline, deterministic with seed=0 |
| MP3 decoding | miniaudio | pydub+ffmpeg, soundfile | No external binary dependencies, single pip install |

---

## Known Limitations

- **Neo4j startup latency:** If Neo4j is slow to start, entity tools log a warning and return "unavailable" rather than crashing. Restart assistant after Neo4j is ready.
- **Tool results not persisted in ConversationMemory:** Email IDs, full content, and other tool outputs exist only within a single `chat_async` call. Follow-up turns re-fetch by subject/sender. A future improvement could optionally store compact tool summaries in memory.
- **crawl4ai deep_crawling import:** `BFSDeepCrawlStrategy` is in `crawl4ai.deep_crawling` which may not exist in older versions. `WebCrawler` falls back to single-page fetch gracefully.
- **Conversation summarisation frequency:** `summarize_and_store` runs after every single assistant turn, making one additional LLM API call per turn. Could be throttled to every N turns for high-frequency use.
- **No speaker interruption:** If a scheduled task completes while the assistant is speaking, the result is only stored — it will not interrupt or queue after current speech.
- **Context window for long crawls:** `_TOTAL_CHAR_LIMIT = 40_000` characters from web crawls. Very long crawls are truncated.
- **Language detection on short/mixed text:** Responses shorter than 60 chars or with >30% ASCII words always use the English voice. Purely foreign-language responses of sufficient length are routed correctly.
- **Whisper model size vs accuracy trade-off:** `small-int8` balances speed and multilingual accuracy. For better Lithuanian/Polish/Russian recognition, upgrade to `medium-int8` in `docker-compose.yml` at the cost of higher latency and ~1.5 GB model download.

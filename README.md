# JarvisLT

A local-first, voice-activated personal assistant with long-term memory, scheduled tasks, and web crawling. Runs entirely on your machine — only the LLM inference and optional MCP servers reach the internet.

---

## Features

| Capability | How |
|---|---|
| Wake word detection | `hey_jarvis` via wyoming-openwakeword |
| Speech-to-text | wyoming-faster-whisper (local, multilingual auto-detect) |
| Text-to-speech | speaches (Kokoro-82M, local) + edge-tts for Lithuanian/Polish/Russian |
| Multilingual support | Automatic language detection — reads Lithuanian, Polish, Russian natively |
| LLM reasoning | OpenAI gpt-4o with full tool-use loop |
| Multi-turn conversations | Stays active after each response (60s follow-up window, no wake word needed) |
| Short-term memory | Sliding JSON window (last 20 turns, persists across restarts) |
| Long-term entity memory | Neo4j graph (people, events, preferences, relationships) |
| Semantic search | ChromaDB with local embeddings (all-MiniLM-L6-v2) |
| Scheduled tasks | APScheduler — cron or interval, defined in config or by voice |
| Web crawling | crawl4ai + Playwright — JS-heavy pages, configurable depth |
| Interactive browsing | @playwright/mcp — LLM-driven navigation, clicking, form filling |
| Web search | Serper.dev — real Google search results, 2500 free/month |
| Places search | Google Places API — restaurants, shops, POIs with ratings and hours |
| Home automation | Home Assistant REST API |
| Extensible tools | Any MCP server added to config.yaml is auto-discovered |

---

## Prerequisites

- Python 3.10+
- Node.js 18+ (for MCP servers)
- Docker + Docker Compose
- `pipenv`

---

## Setup — Windows (press-and-forget installer)

> **Requires Windows 10 1809+ or Windows 11** (winget must be available).

```powershell
# 1. Open PowerShell in the JarvisLT folder and run:
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer will:
1. Install Python 3.10, Node.js, and Docker Desktop via `winget`
2. Run `pipenv install`
3. Prompt for API keys and write `.env`
4. Start all Docker containers
5. Install Playwright Chromium
6. Open a browser for one-time Google OAuth (Gmail/Calendar)

**If Docker Desktop triggers a reboot**, re-run with:
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -SkipPrereqs
```

Once setup is complete, **double-click `start.bat`** to launch Jarvis.

**Flags:**

| Flag | Effect |
|---|---|
| `-SkipPrereqs` | Skip winget installs (Python/Node/Docker already installed) |
| `-SkipDocker` | Skip Docker startup and `docker compose up` |
| `-SkipOAuth` | Skip Google OAuth step (run `pipenv run python auth_google.py` later) |

**Windows troubleshooting:**
- *No microphone input* → Windows Settings → Privacy & Security → Microphone → allow access
- *Docker not found after install* → reboot required, then re-run with `-SkipPrereqs`
- *MCP server errors* → run `pipenv run python main.py` from the JarvisLT folder (not from another directory)

---

## Setup — Linux / macOS

### 1. Clone and install

```bash
git clone git@github.com:vitalij555/JarvisLT.git
cd JarvisLT
pipenv install
```

> **macOS only:** if `sounddevice` fails, run `brew install portaudio` first.
> Also grant microphone access: System Preferences → Privacy & Security → Microphone.

### 2. Environment variables

```bash
cp .env.example .env
# Edit .env and fill in:
#   OPENAI_API_KEY=sk-...          (required)
#   NEO4J_PASSWORD=jarvispass      (or your chosen password)
#   HA_TOKEN=...                   (optional, for Home Assistant)
#   SERPER_API_KEY=...             (optional, for Google search — serper.dev)
#   GOOGLE_PLACES_API_KEY=...      (optional, for restaurant/POI search)
```

### 3. Start Docker services

```bash
docker compose up -d
```

This starts:
- **speaches** — TTS on port 8000
- **wyoming-whisper** — STT on port 10300 (multilingual, auto language detect)
- **wyoming-openwakeword** — wake word on port 10400
- **neo4j** — graph memory on ports 7474 (browser) and 7687 (Bolt)

> **Note:** On first start, wyoming-whisper downloads the `small-int8` Whisper model (~500 MB). This is a one-time download.

### 4. Install Playwright browsers

Required for web crawling and the @playwright/mcp server:

```bash
# For crawl4ai (Python)
pipenv run crawl4ai-setup
# If the above fails on sudo, run manually:
pipenv run python -m playwright install chromium

# For @playwright/mcp (Node) — no pre-install needed, npx fetches on demand
```

### 5. Google OAuth (for Gmail/Calendar MCP)

```bash
python auth_google.py
```

Follow the browser prompt. Credentials are saved to `.oauth2.<email>.json` (gitignored).

### 6. Run

```bash
pipenv run python main.py
```

Say **"Hey Jarvis"** to activate.

---

## Conversation Flow

```
"Hey Jarvis"  →  Jarvis: "Yes?"  →  [speak your command]
                                          ↓
                                    Jarvis responds
                                          ↓
                               [60-second follow-up window]
                              ┌───────────────────────────┐
                              │  Speak follow-up → Jarvis │
                              │  responds → 60s window    │  ← repeats
                              └───────────────────────────┘
                                          ↓ (silence for 60s)
                              Jarvis: "Going to sleep. Say 'Hey Jarvis'..."
```

No need to repeat the wake word for follow-up questions. Jarvis maintains full conversation context across turns within a session and across restarts (last 20 turns persisted to JSON).

---

## Multilingual Support

Jarvis automatically detects the language of text and selects the appropriate voice:

| Language | STT | TTS Engine | Default Voice |
|---|---|---|---|
| English | Whisper auto | speaches / Kokoro-82M | `af_heart` |
| Lithuanian | Whisper auto | edge-tts | `lt-LT-OnaNeural` |
| Polish | Whisper auto | edge-tts | `pl-PL-ZofiaNeural` |
| Russian | Whisper auto | edge-tts | `ru-RU-SvetlanaNeural` |

You can mix languages naturally:
- *"Find the last email called Penktadienio laiškas"* — Whisper transcribes the mixed input; Gmail is queried correctly.
- Emails written in Lithuanian are read back with a Lithuanian voice automatically.

To change voices, edit `multilingual_voices` in `config.yaml`. List available voices:
```bash
python -m edge_tts --list-voices | grep -E "^lt|^pl|^ru"
```

---

## Configuration

All configuration lives in `config.yaml`. No code changes are needed to add MCP servers, change TTS voice, or define scheduled tasks.

### Key audio settings

```yaml
audio:
  silence_threshold: 100    # RMS sensitivity — lower = picks up quieter voices
  silence_duration: 1.5     # seconds of silence to end speech capture
  speech_timeout: 8.0       # seconds to wait after "Yes?" before giving up
  conversation_timeout: 60.0 # seconds to wait for follow-up before going to sleep
```

### Adding a scheduled task (static)

```yaml
scheduled_tasks:
  mastercard_news:
    cron: "0 9 * * 1"       # every Monday at 9am
    prompt: |
      Use web_crawl on https://www.mastercard.com/us/en/news-and-trends/stories.html
      with max_depth=2 and max_pages=15.
      Summarise each story: title, 2-3 sentences, article URL.
      Today is {date}.
    enabled: true
```

Restart required for static task changes. Tasks created by voice are persisted in `jarvis_tasks.db` and survive restarts automatically.

### Adding an MCP server

```yaml
mcp_servers:
  my_server:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: ""   # or set in .env
```

Jarvis discovers all tools from the server automatically on startup.

---

## Voice Commands

### Email

| Say | What happens |
|---|---|
| *"Find the last email from KMM school"* | Keyword search in Gmail; web-search fallback to resolve domain if needed |
| *"Find the email called Penktadienio laiškas"* | Gmail search with Lithuanian subject; auto-detected and read in Lithuanian |
| *"Summarize it in 5 sentences"* | Re-fetches the email found in the previous turn; no need to repeat yourself |
| *"Record that as a task named Friday email, run every Friday at 9am"* | Saves the last action as a scheduled task |

### Memory

| Say | What happens |
|---|---|
| *"Remember that my daughter Sofia goes to Vilnius Gymnasium"* | Stored in Neo4j as entity + fact |
| *"What do you know about Sofia?"* | Recalled from Neo4j |
| *"Remember that I prefer jazz music"* | Stored as preference entity |
| *"What did we talk about last week regarding the dentist?"* | Semantic search in ChromaDB |
| *"Forget what you know about X"* | Deleted from Neo4j |

### Scheduled Tasks

| Say | What happens |
|---|---|
| *"Create a task: every Friday check Gmail for school emails and summarise"* | Task created in APScheduler + saved to DB |
| *"List all my tasks"* | Lists tasks with next run times |
| *"Disable the morning news task"* | Task paused (not deleted) |
| *"What happened while I slept?"* | Returns task results from last 8 hours |
| *"Delete the server check task"* | Removed from DB and scheduler |

### Web & Search

| Say | What happens |
|---|---|
| *"Search for the latest news about quantum computing"* | Google search via Serper |
| *"What is the current population of Lithuania?"* | Google search — returns answer box directly |
| *"Fetch the BBC news homepage and summarise it"* | Single-page crawl via crawl4ai |
| *"Check the Mastercard news page and list all articles"* | depth=2 crawl (listing + each article) |
| *"Go to bbc.com and click on the Technology section"* | Interactive via @playwright/mcp |

### Places

| Say | What happens |
|---|---|
| *"Find Italian restaurants near Gedimino 1, Vilnius"* | Google Places search with radius |
| *"Find coffee shops near me open now"* | Uses stored home address from memory |
| *"Find a pharmacy nearby"* | Places search with open/closed status |

> **Tip:** Store your home address once so "nearby" always works:
> *"Hey Jarvis, remember that my home address is Gedimino pr. 1, Vilnius"*

---

## Project Structure

```
JarvisLT/
├── main.py                        Entry point — loads config, starts assistant
├── config.yaml                    All configuration (services, LLM, tasks, MCP servers)
├── .env                           Secrets (gitignored)
├── docker-compose.yml             TTS, STT, wake word, Neo4j
│
├── jarvis/
│   ├── core/
│   │   └── assistant.py           Main async loop — wake word, conversation mode, wires all components
│   │
│   ├── audio/
│   │   ├── wake_word.py           Wyoming wake word client
│   │   ├── listener.py            Wyoming STT + silence-based VAD + speech timeout
│   │   └── speaker.py             speaches TTS + edge-tts multilingual + sounddevice playback
│   │
│   ├── llm/
│   │   ├── claude_client.py       OpenAI client — tool-use loop, MCP, local tool registry
│   │   └── memory.py              Short-term conversation history (JSON, sliding window)
│   │
│   ├── memory/
│   │   ├── entity_store.py        Neo4j — entities, facts, relationships
│   │   ├── vector_store.py        ChromaDB — conversation summaries + task results
│   │   ├── memory_manager.py      Facade — hybrid recall, background summarisation
│   │   └── memory_tools.py        LLM tool schemas: remember/recall/search/forget
│   │
│   ├── scheduler/
│   │   ├── task_store.py          aiosqlite — task definitions + run history
│   │   ├── task_runner.py         APScheduler + HeadlessSession (LLM without audio)
│   │   └── task_tools.py          LLM tool schemas: create/list/delete/results
│   │
│   └── connectors/
│       ├── home_assistant.py      HA REST — get_state, call_service, list_entities
│       ├── web_crawler.py         crawl4ai depth crawler (Playwright backend)
│       ├── web_tools.py           LLM tool schema: web_crawl
│       ├── search_tools.py        LLM tool schema: google_search (Serper.dev)
│       └── places_tools.py        LLM tool schema: search_places (Google Places API)
│
└── jarvis_tasks.db                SQLite — task definitions + run history (gitignored)
```

---

## Data Storage

| Store | Technology | Contents | Location |
|---|---|---|---|
| Short-term memory | JSON file | Last 20 conversation turns | `conversation_history.json` |
| Entity memory | Neo4j | People, places, events, preferences, relationships | Docker volume `neo4j_data` |
| Semantic memory | ChromaDB | Conversation summaries, task results (embeddings) | `.chroma/` |
| Task store | SQLite (aiosqlite) | Task definitions and run history | `jarvis_tasks.db` |

---

## Services Overview

| Service | Port | Purpose |
|---|---|---|
| speaches (Docker) | 8000 | TTS — OpenAI-compatible REST, Kokoro-82M voice |
| wyoming-whisper (Docker) | 10300 | STT — Wyoming TCP, multilingual auto-detect |
| wyoming-openwakeword (Docker) | 10400 | Wake word — Wyoming TCP protocol |
| Neo4j (Docker) | 7474, 7687 | Graph DB — long-term entity memory |
| Neo4j browser | 7474 | Web UI to inspect graph data |

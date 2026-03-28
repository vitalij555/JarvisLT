# JarvisLT

A local-first, voice-activated personal assistant with long-term memory, scheduled tasks, and web crawling. Runs entirely on your machine — only the LLM inference and optional MCP servers reach the internet.

---

## Features

| Capability | How |
|---|---|
| Wake word detection | `hey_jarvis` via wyoming-openwakeword |
| Speech-to-text | wyoming-faster-whisper (local) |
| Text-to-speech | speaches (Kokoro-82M, local) |
| LLM reasoning | OpenAI gpt-4o with full tool-use loop |
| Short-term memory | Sliding JSON window (last 20 turns) |
| Long-term entity memory | Neo4j graph (people, events, preferences, relationships) |
| Semantic search | ChromaDB with local embeddings (all-MiniLM-L6-v2) |
| Scheduled tasks | APScheduler — cron or interval, defined in config or by voice |
| Web crawling | crawl4ai + Playwright — JS-heavy pages, configurable depth |
| Interactive browsing | @playwright/mcp — LLM-driven navigation, clicking, form filling |
| Home automation | Home Assistant REST API |
| Extensible tools | Any MCP server added to config.yaml is auto-discovered |

---

## Prerequisites

- Python 3.10+
- Node.js 18+ (for MCP servers)
- Docker + Docker Compose
- `pipenv`

---

## Setup

### 1. Clone and install

```bash
git clone git@github.com:vitalij555/JarvisLT.git
cd JarvisLT
pipenv install
```

### 2. Environment variables

```bash
cp .env.example .env
# Edit .env and fill in:
#   OPENAI_API_KEY=sk-...
#   HA_TOKEN=...              (optional, for Home Assistant)
#   NEO4J_PASSWORD=jarvispass (or your chosen password)
```

### 3. Start Docker services

```bash
docker compose up -d
```

This starts:
- **speaches** — TTS on port 8000
- **wyoming-whisper** — STT on port 10300
- **wyoming-openwakeword** — wake word on port 10400
- **neo4j** — graph memory on ports 7474 (browser) and 7687 (Bolt)

### 4. Install Playwright browsers

Required for web crawling and the @playwright/mcp server:

```bash
# For crawl4ai (Python)
crawl4ai-setup

# For @playwright/mcp (Node)
npx playwright install chromium
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

## Configuration

All configuration lives in `config.yaml`. No code changes are needed to add MCP servers, change TTS voice, or define scheduled tasks.

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

### Web

| Say | What happens |
|---|---|
| *"Fetch the BBC news homepage and summarise it"* | Single-page crawl via crawl4ai |
| *"Check the Mastercard news page and list all articles"* | depth=2 crawl (listing + each article) |
| *"Go to bbc.com and click on the Technology section"* | Interactive via @playwright/mcp |

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
│   │   └── assistant.py           Main async loop — wires all components
│   │
│   ├── audio/
│   │   ├── wake_word.py           Wyoming wake word client
│   │   ├── listener.py            Wyoming STT + silence-based VAD
│   │   └── speaker.py             speaches TTS + sounddevice playback
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
│       └── web_tools.py           LLM tool schema: web_crawl
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
| wyoming-whisper (Docker) | 10300 | STT — Wyoming TCP protocol |
| wyoming-openwakeword (Docker) | 10400 | Wake word — Wyoming TCP protocol |
| Neo4j (Docker) | 7474, 7687 | Graph DB — long-term entity memory |
| Neo4j browser | 7474 | Web UI to inspect graph data |

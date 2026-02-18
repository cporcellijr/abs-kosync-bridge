# Changelog

<!-- markdownlint-disable MD024 -->

All notable changes to ABS-KoSync Enhanced will be documented in this file.

## [6.3.0] - 2026-02-18

### 🚀 Features

- **Tri-Link Architecture**: Maintain a three-way link between ABS audiobook, KOReader ebook, and Storyteller entries.
- **Auto-Forge Pipeline**: Automated downloading, staging, and hand-off to Storyteller for processing.
- **Hardcover.app Audiobook Support**: Link specific editions and sync listening progress (in seconds).
- **Booklore & CWA (OPDS) Integration**: Fetch ebooks from Booklore and OPDS sources.
- **Split-Port Security Mode**: Run sync and admin UI on separate ports.
- **New Transcription Providers**: Support for Whisper.cpp Server, Deepgram API, and CUDA GPU acceleration.
- **Progress Suggestions**: Smart auto-discovery and suggestions for potential matches.
- **UI Redesign**: Horizontal dashboard cards, overhauled match pages, and responsive settings UI.

### 🐛 Fixes

- Fixed KOReader sync crashes (XPath double `body` tag issue).
- Fixed KOSync hash overwrites by Storyteller artifacts.
- Fixed race conditions in Storyteller ingestion.
- Fixed special characters in filenames breaking glob searches.
- Fixed KOSync client headers, legacy exception types, and sync position payloads.

### 🧹 Maintenance

- **Logging Standardization**: Consistent emoji prefixes and log levels across the entire codebase.
- **Unified DB Architecture**: Transitioned to SQLAlchemy for alignments, transcripts, and settings.
- **Alembic Migrations**: Improved migration tracking and safety checks.
- **Storyteller API**: Removed direct DB access in favor of strictly API-based communication.

---

## [6.2.0] - 2026-02-13

### 🚀 Features

#### Suggestion Logic (`b8527a4`)

- Implemented core logic for `PendingSuggestion`
- Added fallback matching using `difflib` for fuzzy text matching when exact matches fail
- Added `SuggestionManager` service to handle auto-discovery of unmapped books

### 🐛 Fixes

#### Sync Path Fallback & XPath Support (`5a57355`)

- Fixed `_get_sync_path` to properly handle `None` values
- Added XPath support for more accurate position tracking in KOReader
- Improved fallback logic when checking multiple sync paths

---

## [4.0.0] - 2024-12-31

### 🚀 Major: Storyteller REST API Integration

**Breaking Change:** Storyteller sync now uses the REST API instead of direct SQLite writes. This prevents the mobile app from overwriting synced positions.

#### Added

- **Storyteller REST API client** (`storyteller_api.py`)
  - Authenticates via `/api/token` endpoint
  - Updates positions via `/api/books/{uuid}/positions`
  - Auto-refreshes tokens (30-second expiry)
  - Falls back to SQLite if API credentials not configured
  
- **New environment variables:**
  - `STORYTELLER_API_URL` - Storyteller server URL (e.g., `http://host.docker.internal:8001`)
  - `STORYTELLER_USER` - Storyteller username
  - `STORYTELLER_PASSWORD` - Storyteller password

#### Changed

- `main.py` now imports from `storyteller_api` with SQLite fallback
- Dockerfile updated to include `storyteller_api.py`
- Startup logs now indicate which Storyteller mode is active (API vs SQLite)

#### Fixed

- **Mobile app overwrite issue** - Storyteller mobile app's 8-second sync cycle can no longer overwrite positions set by the sync daemon
- Uses timestamp leapfrog strategy for conflict resolution

---

## [3.0.0] - 2024-12-30

### 🚀 Major: Hardcover Integration

#### Added

- **Hardcover.app integration** (`hardcover_client.py`)
  - Auto-matches books by ISBN or title/author
  - Syncs reading progress to Hardcover
  - Updates reading status (Currently Reading → Finished)
  - Delta-based sync - only updates when progress changes >1%

- **New environment variable:**
  - `HARDCOVER_TOKEN` - API token from hardcover.app/account/api

#### Changed

- Sync cycle now includes Hardcover as fourth sync target
- Books are auto-matched to Hardcover on first sync

---

## [2.0.0] - 2024-12-28

### 🚀 Major: Three-Way Sync & Web UI

#### Added

- **Three-way synchronization** between ABS, KOSync, and Storyteller
- **Web management interface** on port 5757
  - Dashboard with progress visualization
  - Single match interface with cover art
  - Batch matching queue system
  - Book Linker for Storyteller processing workflow
  - Suggestions page for auto-discovered matches

- **Suggestion Manager** (`suggestion_manager.py`)
  - Auto-discovers unmapped books with activity
  - Fuzzy matches audiobooks to ebooks
  - Presents suggestions for user approval

- **Book Linker workflow**
  - Search and select ebooks + audiobooks
  - Auto-copy to Storyteller processing folder
  - Monitor for completed readaloud files
  - Auto-cleanup after processing

#### Changed

- Uses `token_sort_ratio` for more accurate fuzzy matching
- LRU cache (capacity=3) prevents memory issues with large libraries
- Thread-safe JSON database with file locking

---

## [1.0.0] - 2024-12-25

### 🎉 Initial Release

#### Features

- Two-way sync between Audiobookshelf and KOSync
- AI-powered transcription using Whisper
- Fuzzy text matching for position alignment
- Docker containerization
- Auto-add to ABS collections
- Booklore shelf integration

---

## Migration Guide

### Upgrading to 4.0.0

1. **Add new environment variables** to your `docker-compose.yml`:

   ```yaml
   - STORYTELLER_API_URL=http://host.docker.internal:8001
   - STORYTELLER_USER=your_username
   - STORYTELLER_PASSWORD=your_password
   ```

2. **Rebuild the container:**

   ```bash
   docker compose down
   docker compose build --no-cache
   docker compose up -d
   ```

3. **Verify API mode** in logs:

   ```text
   ✅ Storyteller API connected at http://host.docker.internal:8001
   Using Storyteller REST API for sync
   ```

If you see "Using Storyteller SQLite fallback", check your credentials.

### Upgrading to 3.0.0

1. Add `HARDCOVER_TOKEN` environment variable
2. Rebuild container
3. Existing mappings will auto-match to Hardcover on next sync

---

## Environment Variables Reference

<!-- markdownlint-disable MD060 -->

> [!NOTE]
> All settings below can be configured via the **Web UI** at `/settings`. Environment variables are only used for initial bootstrapping on first launch.

### Audiobookshelf (Required)

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_SERVER` | — | Audiobookshelf server URL |
| `ABS_KEY` | — | ABS API token |
| `ABS_LIBRARY_ID` | — | ABS library ID to sync from |
| `ABS_COLLECTION_NAME` | `Synced with KOReader` | Name of the ABS collection to auto-add synced books to |
| `ABS_PROGRESS_OFFSET_SECONDS` | `0` | Rewind progress sent to ABS by this many seconds |
| `ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID` | `false` | Limit ebook searches to the configured ABS library only |

### KOSync

| Variable | Default | Description |
|----------|---------|-------------|
| `KOSYNC_ENABLED` | `false` | Enable KOSync integration |
| `KOSYNC_SERVER` | — | Target KOSync server URL |
| `KOSYNC_USER` | — | KOSync username |
| `KOSYNC_KEY` | — | KOSync password |
| `KOSYNC_HASH_METHOD` | `content` | Hash method: `content` (accurate) or `filename` (fast) |
| `KOSYNC_USE_PERCENTAGE_FROM_SERVER` | `false` | Use raw % from server instead of text-based matching |

### Storyteller

| Variable | Default | Description |
|----------|---------|-------------|
| `STORYTELLER_ENABLED` | `false` | Enable Storyteller integration |
| `STORYTELLER_API_URL` | — | Storyteller server URL (e.g., `http://host.docker.internal:8001`) |
| `STORYTELLER_USER` | — | Storyteller username |
| `STORYTELLER_PASSWORD` | — | Storyteller password |

### Booklore

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOKLORE_ENABLED` | `false` | Enable Booklore integration |
| `BOOKLORE_SERVER` | — | Booklore server URL |
| `BOOKLORE_USER` | — | Booklore username |
| `BOOKLORE_PASSWORD` | — | Booklore password |
| `BOOKLORE_SHELF_NAME` | `Kobo` | Name of the Booklore shelf to auto-add synced books to |
| `BOOKLORE_LIBRARY_ID` | — | Restrict sync to a specific Booklore library ID |

### CWA (Calibre-Web Automated)

| Variable | Default | Description |
|----------|---------|-------------|
| `CWA_ENABLED` | `false` | Enable CWA/OPDS integration |
| `CWA_SERVER` | — | Calibre-Web server URL |
| `CWA_USERNAME` | — | Calibre-Web username |
| `CWA_PASSWORD` | — | Calibre-Web password |

### Hardcover.app

| Variable | Default | Description |
|----------|---------|-------------|
| `HARDCOVER_ENABLED` | `false` | Enable Hardcover.app integration |
| `HARDCOVER_TOKEN` | — | API token from hardcover.app/account/api |

### Telegram Notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_ENABLED` | `false` | Enable Telegram notifications |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID to send messages to |
| `TELEGRAM_LOG_LEVEL` | `ERROR` | Minimum log level to forward (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`) |

### Shelfmark

| Variable | Default | Description |
|----------|---------|-------------|
| `SHELFMARK_URL` | — | URL to your Shelfmark instance (enables nav icon when set) |

### Sync Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNC_PERIOD_MINS` | `5` | Background sync interval in minutes |
| `SYNC_DELTA_ABS_SECONDS` | `60` | Min ABS progress change (seconds) to trigger an update |
| `SYNC_DELTA_KOSYNC_PERCENT` | `0.5` | Min KOSync progress change (%) to trigger an update |
| `SYNC_DELTA_KOSYNC_WORDS` | `400` | Min word-count change to trigger a KOSync update |
| `SYNC_DELTA_BETWEEN_CLIENTS_PERCENT` | `0.5` | Min difference between clients (%) to trigger propagation |
| `FUZZY_MATCH_THRESHOLD` | `80` | Text matching confidence threshold (0–100) |
| `SYNC_ABS_EBOOK` | `false` | Also sync progress to the ABS ebook item |
| `XPATH_FALLBACK_TO_PREVIOUS_SEGMENT` | `false` | Fall back to previous XPath segment on lookup failure |
| `SUGGESTIONS_ENABLED` | `false` | Enable auto-discovery suggestions |

### Transcription

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSCRIPTION_PROVIDER` | `local` | Provider: `local` (faster-whisper), `deepgram`, or `whisper_cpp` |
| `WHISPER_MODEL` | `tiny` | Whisper model size (`tiny`, `base`, `small`, `medium`, `large`) |
| `WHISPER_DEVICE` | `auto` | Device: `auto`, `cpu`, or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `auto` | Precision: `int8`, `float16`, `float32` |
| `WHISPER_CPP_URL` | — | URL to whisper.cpp server endpoint |
| `DEEPGRAM_API_KEY` | — | Deepgram API key |
| `DEEPGRAM_MODEL` | `nova-2` | Deepgram model tier |

### System

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `America/New_York` | Container timezone |
| `LOG_LEVEL` | `INFO` | Application log level |
| `DATA_DIR` | `/data` | Path to persistent data directory |
| `BOOKS_DIR` | `/books` | Path to local ebook library |
| `AUDIOBOOKS_DIR` | `/audiobooks` | Path to local audiobook files |
| `STORYTELLER_LIBRARY_DIR` | `/storyteller_library` | Path to Storyteller library directory |
| `EBOOK_CACHE_SIZE` | `3` | LRU cache size for parsed ebooks |
| `JOB_MAX_RETRIES` | `5` | Max transcription job retry attempts |
| `JOB_RETRY_DELAY_MINS` | `15` | Minutes to wait between job retries |

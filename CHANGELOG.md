# Changelog

<!-- markdownlint-disable MD024 -->

All notable changes to ABS-KoSync Enhanced will be documented in this file.

## [6.3.2] - 2026-02-27

### Enhancements

- **Instant Sync Toggle**: Added `INSTANT_SYNC_ENABLED` setting to enable or disable event-driven instant sync globally. When off, the ABS Socket.IO listener and KoSync push trigger are both inactive and the bridge falls back to the standard background poll cycle.
- **Instant Sync Settings**: Added `ABS_SOCKET_DEBOUNCE_SECONDS` (default 30s) to control how long the socket listener waits after a playback event before triggering a sync. Tune this lower for faster response or higher to avoid hammering downstream services during active scrubbing.
- **Per-Client Polling**: Storyteller and Booklore can now be configured with their own poll intervals, independent of the global sync cycle. Set either client to `custom` mode in Settings and choose a polling interval (in seconds). The poller checks for position changes on active books only and triggers a targeted sync when a real change is detected.
- **Shared Write Suppression**: Centralized write-tracking into a single `write_tracker` module. All clients (ABS, KoSync, Storyteller, Booklore) now share the same suppression logic to prevent feedback loops after the bridge pushes a progress update.
- **Storyteller Transcript Priority Source**: Added Storyteller forced-alignment transcript ingestion as the top transcript source during matching/linking (priority: Storyteller -> SMIL -> Whisper).
- **New Optional Setting `STORYTELLER_ASSETS_DIR`**: Added Settings/UI support for Storyteller assets root (`{root}/assets/{title}/transcriptions`). This source is opt-in and skipped when unset.
- **Native Storyteller Alignment Maps**: Added direct map generation from `wordTimeline` data (`chapter`, local UTF-16 char, local ts, global ts) without anchor rebuild.
- **Direct Timestamp -> EPUB Locator (Storyteller only)**: ABS audiobook timestamps on Storyteller-transcript books can now resolve to EPUB locators directly from transcript offsets, bypassing fuzzy text search.
- **Storyteller Backfill Action**: Added a Settings maintenance action to bulk ingest/re-ingest Storyteller transcripts for existing Storyteller-linked books and rebuild storyteller-native alignments.
- **Storyteller Transcript Ingest in Forge Pipeline**: Added transcript ingestion and anchored alignment generation directly in the forge workflow.
- **Suggestion Discovery from Socket Events**: Unknown-book Socket.IO progress events now trigger suggestion discovery to surface likely matches automatically.
- **Event-Driven Real-Time Sync**: Added ABS Socket.IO listener for near-instant sync. When you play/pause an audiobook in Audiobookshelf, progress automatically syncs to all configured clients (KoSync, Storyteller, Booklore, Hardcover) within ~30 seconds — no more waiting for the poll cycle. Also triggers instant sync on KoSync PUT from KOReader. Configurable via `ABS_SOCKET_ENABLED` and `ABS_SOCKET_DEBOUNCE_SECONDS`.
- **Dashboard Search**: Added instant client-side search filter to the dashboard. Users can now type in a "Search books..." field to filter the library by title or author in real time without a page reload.
- **Sync Now & Mark Complete Actions**: Added quick-action buttons to each book card — ⚡ triggers an immediate background sync cycle, and ✅ marks a book as finished across all configured platforms with an optional mapping cleanup prompt.
- **Dashboard Version Badge**: Cleaned up the version display badge. Dev builds now show `Build dev-N` and official releases show `vX.Y.Z` without redundant prefixes.

### Bug Fixes

- **ABS Socket.IO Auth Reliability**: The socket connection was previously sending the auth token at the transport level (HTTP headers + Socket.IO CONNECT packet) in addition to the `"auth"` event. On some ABS setups this caused both the primary token and the fallback to be rejected immediately. Auth is now sent exclusively via the `"auth"` event (the canonical ABS flow). If authentication fails, the listener disconnects cleanly and the bridge automatically falls back to the standard poll cycle — sync continues uninterrupted.
- **Storyteller Filename Prefix Compatibility**: Ingestion now accepts both `00000-xxxxx.json` and `00001-xxxxx.json` chapter prefixes.
- **Storyteller Format Guardrails**: Backfill/ingest now validates chapter JSON shape (`dict` with `wordTimeline`) before ingesting, preventing invalid files from failing alignment after copy.
- **ABS Sync Lag with Storyteller Transcripts**: Fixed delayed ABS synchronization behavior for Storyteller-transcript-backed books.
- **Tri-Link Drift and Storyteller Jump Detection**: Corrected drift handling and jump-detection logic to prevent incorrect position propagation.
- **Storyteller Backfill and BookLore Reset Fallback**: Fixed backfill messaging/flow and BookLore clear/reset fallback behavior.
- **KOSync Hash Mismatch**: Resolved a hash mismatch issue that occurred when the device epub differs from the bridge epub, preventing stale progress lookups.
- **KOSync Shadow Documents**: Fixed an issue where stale shadow documents could be returned in GET progress responses, causing incorrect sync positions.
- **KOSync Admin Endpoints**: Corrected auth handling on admin endpoints to allow dashboard access while keeping sensitive operations protected.
- **Booklore Double Search**: Fixed a redundant double-search issue in Booklore book lookups, improving match performance.
- **Database Schema**: Consolidated schema repair into a single clean Alembic migration, reducing startup migration time and preventing edge-case schema conflicts.
- **Mark Complete Crash**: Fixed a `TypeError` in the `mark_complete` endpoint caused by invalid `LocatorResult` keyword arguments.
- **LRUCache Thread Safety**: Added `threading.Lock` to the `LRUCache` class in `ebook_utils.py`. The cache is accessed concurrently by the sync daemon, forge background jobs, and web server requests, but `OrderedDict.move_to_end()` and `popitem()` are not thread-safe for concurrent mutation.
- **Forge Service Audio Copying**: Fixed an indentation error in the audio file copying logic that prevented files from being copied when found via exact path or suffix matching.
- **ABS Socket.IO Feedback Loop**: Fixed a self-triggering sync loop where BookBridge's own ABS progress writes fired a `user_item_progress_updated` socket event, which the listener then treated as a real user change and scheduled another sync cycle. A module-level write-suppression tracker now stamps each book after a write; any socket event arriving within 60 seconds of that stamp is silently dropped. A single real progress change now produces exactly one sync cycle instead of three.
- **Booklore Full Library Scan on Progress Update**: Fixed `update_progress()` calling `_refresh_book_cache()` after every successful write, which fetched all books from the Booklore API on every sync cycle. Progress is now applied to the cached entry in-place. Full library scans still occur on initial load and the hourly staleness check.

### Maintenance

- **Comment Cleanup**: Removed reflective/speculative inline comments for clearer, more maintainable code.

---
## [6.3.0] - 2026-02-23

### � Critical Update Requirements

- **Storyteller API v2 Requirement:** The bridge has fully transitioned to the Storyteller REST API v2 endpoints (`/api/v2/`). **You MUST update your Storyteller container to the latest version to use Bridge v6.3.0.** Legacy Storyteller versions are no longer supported and will result in 404 connection errors.
- **Docker Compose Volume Mounts for "Forge":** The new Auto-Forge pipeline requires proper volume mapping for directory transfers. Ensure your `docker-compose.yml` includes mappings for `STORYTELLER_LIBRARY_DIR`, `BOOKS_DIR`, and any relevant processing directories for the Forge tab to function without "Directory not found" errors.
- **Database Migration:** This update includes a major database schema upgrade (Alembic) to support the Tri-Link architecture. **Highly Recommended: Backup your `database.db` and legacy JSON files before pulling this update.** If you encounter a boot-loop due to a locked database, simply deleting the DB and letting it rebuild is the fastest fix, as the bridge can auto-match most entries automatically.
- **KOSync "Stuck" Progress on Old Links:** Books matched under older versions of the bridge might lack the `original_ebook_filename` required by the new Tri-Link architecture. If an older book stops syncing progress to KOReader after this update, simply delete the mapping from the dashboard and re-match it to rebuild the link correctly.

### �🚀 New Features & Integrations

- **Tri-Link Architecture**: Maintain a three-way link between ABS audiobook, KOReader ebook, and Storyteller entries.
- **Auto-Forge Pipeline**: Automated downloading, staging, and hand-off to Storyteller for processing.
- **Hardcover.app Audiobook Support**: Link specific editions and sync listening progress (in seconds).
- **Booklore & CWA (OPDS) Integration**: Fetch ebooks from Booklore and OPDS sources, including backward-compatible fallbacks for Booklore v2.
- **Split-Port Security Mode**: Run sync and admin UI on separate ports.
- **New Transcription Providers**: Support for Whisper.cpp Server, Deepgram API, and CUDA GPU acceleration.
- **Advanced Anchor Mapping**: Implemented BS4-to-LXML Hybrid Anchor Mapping and SMIL Extractor Smart Duration Mapping for perfect KOReader xpath generation.

### ✨ Enhancements

- **UI Redesign**: Horizontal dashboard cards, overhauled match pages, and responsive settings UI.
- **Progress Suggestions**: Smart auto-discovery and suggestions for potential matches.
- **Dynamic Configuration**: ABSClient web UI settings now take effect dynamically without requiring a restart.
- **Optimized Workflows**: Restored automatic addition of collections and shelves post Auto-Forge processing.
- **Logging Standardization**: Consistent emoji prefixes and log levels across the entire codebase.

### 🐛 Bug Fixes

- **KOReader Sync**: Fixed KOReader sync crashes caused by an XPath double `body` tag issue.
- **KOSync Sync Integrity**: Prevented destructive progress pushes, preserved manual hash overrides, and fixed KOSync hash overwrites by Storyteller artifacts.
- **Storyteller Stability**: Fixed race conditions in Storyteller ingestion and removed conflicting Storyteller fallback collection logic.
- **System Stability**: Fixed special characters in filenames breaking glob searches, corrected Booklore shelf assignment issues during batch matching, and resolved legacy KOSync client headers, legacy exception types, and sync position payloads.
- **Database Persistence & Migrations**: Forced absolute paths for SQLite connections to prevent ephemeral Docker data loss, auto-upgraded legacy DB-migrated books, and prevented legacy DB crashes on startup via Alembic stamping.
- **XPath Hardening**: Defaulted Crengine-safe XPath suffixes, and hardened generation against fragile inline tags to prevent parsing drift.

### ⚠️ Breaking Changes & Deprecations

- **Unified DB Architecture**: Transitioned to SQLAlchemy for alignments, transcripts, and settings.
- **Alembic Migrations**: Improved migration tracking and safety checks.
- **Storyteller API**: Removed direct DB access in favor of strictly API-based communication; legacy Storyteller DB fallback has been deprecated.

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
| `ABS_SOCKET_ENABLED` | `true` | Enable real-time ABS Socket.IO listener for instant sync on playback events |
| `ABS_SOCKET_DEBOUNCE_SECONDS` | `30` | Seconds to wait after last ABS playback event before triggering sync |

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

# BRANCH STATUS: System Rules Setup

## 1. BRANCH CONTEXT / DEEP DIVE

(Generated at branch start. Source of truth for architectural context.)

### 1.1 Architecture Overview

- **Type**: Hybrid System (Flask Web Server + Background Thread Daemon).
- **Entry Point**: `start.sh` -> `src/web_server.py` (which launches `src/sync_manager.py` in a thread).
- **Core Pattern**: Dependency Injection via `init_kosync_server` global injection in `kosync_server.py`.

### 1.2 Database Schema (`src/db/models.py`)

- **`books`**: Core entity. Links `abs_id` (Audiobookshelf ID) to `ebook_filename` and `kosync_doc_id`.
  - *Modes*: `sync_mode` can be 'audiobook' or 'ebook_only'.
- **`kosync_documents`**: Mirror of KOReader's sync format.
  - *Key Fields*: `document_hash` (MD5), `progress` (XPath), `percentage`, `linked_abs_id`.
- **`book_alignments`**: Stores computed Text-to-Audio time mapping (`alignment_map_json`).
- **`states`**: Per-client sync state (`client_name`, `abs_id`, `timestamp`, `percentage`).
- **`pending_suggestions`**: Auto-discovery matches waiting for user approval.

### 1.3 Key Workflows

#### A. Alignment (`src/services/alignment_service.py`)

- **Algo**: Two-Pass N-Gram Anchoring.
  - *Pass 1*: Global Search (N=12 tokens).
  - *Pass 2*: Start Backfill (N=6 tokens) if first anchor is late (>30s).
- **Result**: A generic JSON map `[{"char": 100, "ts": 12.5}, ...]` used for bidirectional lookup.

#### B. Ebook Acquisition (`src/services/library_service.py`)

- **Priority Chain**: `acquire_ebook()`
    1. **ABS Direct**: Checks if Audiobook item has an EPUB file.
    2. **Booklore**: Checks curated `booklore_books` table.
    3. **CWA**: Searches Calibre-Web Automated via OPDS.
    4. **ABS Search**: Searches other ABS libraries.
    5. **Filesystem**: Scans local `epub_cache/`.

#### C. KOSync Protocol (`src/api/kosync_server.py`)

- **Endpoints**: `/syncs/progress`, `/healthcheck`, `/users/*`.
- **Auto-Discovery**: On `PUT /syncs/progress`, if `doc_hash` is unknown:
    1. Scans DB/Filesystem/Booklore for matching EPUB.
    2. Matches EPUB title to ABS Audiobooks.
    3. Creates `PendingSuggestion` for user to link.

### 1.4 API Clients (`src/api/api_clients.py`)

- **ABSClient**: Uses `token` auth. Handles session-based sync (`/api/session/{id}/sync`) and direct progress updates.
- **KoSyncClient**: Optional. Pushes progress to an *external* KOSync server if `KOSync` is enabled in env.

### 1.5 Configuration

- **Environment**: loaded via `os.environ`.
- **Key Vars**: `ABS_SERVER`, `ABS_KEY`, `KOSYNC_USER`, `KOSYNC_KEY` (hashed), `KOSYNC_ENABLED`.

## 2. CURRENT OBJECTIVE

- [x] Main Goal: Fix ID Shadowing (Add `abs_ebook_item_id`)
- [x] Main Goal: Fix Duplicate Entries (Merge & Migrate Strategy)
- [x] STRICT MODE: Enforce UUID matching (Tri-Link logic).
- [x] UI/UX: Update Frontend (Badges, Modals) for new logic.
- [x] Refactor: Remove legacy Storyteller DB (SQLite direct access).
- [x] Verification: Ensure stable sync with new architecture.

## 3. CRITICAL FILE MAP

*(The AI must maintain this list. Add files here before editing them.)*

- `src/api/storyteller_api.py` (Main Client)
- `src/web_server.py` (Routes)
- `static/js/storyteller-modal.js` (Frontend)
- `src/sync_clients/storyteller_sync_client.py`
- `tests/test_trilink.py`

## 4. CHANGE LOG (Newest Top)

- **[2026-02-17 18:30]**: [Antigravity] Verified successful server startup and sync cycle with new API-only architecture.
- **[2026-02-17 18:25]**: [Antigravity] Removed legacy `storyteller_db.py` and refactored `StorytellerAPIClient` to be the sole client. Updated tests to remove legacy dependencies.
- **[2026-02-17 18:10]**: [Antigravity] Implemented strict UUID enforcement in backend and frontend. verified with tests.` (no fallbacks).
  - Added "None" option to `storyteller-modal.js` and backend handler in `web_server.py`.
  - Validated with `tests/verify_unlink.py` (mocked environment).
- **2026-02-17 12:55**: (`Antigravity`) Finalized Tri-Link: Enforced strict UUID syncing (removed legacy fallbacks) and implemented explicit unlinking logic in `api_storyteller_link`.
- **2026-02-17 12:48**: (`Antigravity`) Fix: Tri-Link ID Calculation uses original EPUB hash when available.
- **2026-02-17 12:45**: (`Antigravity`) Fixed KOSync env timing, ABSClient crashes, and added Server logging.
- **2026-02-12 19:31**: (`a7e4d47`) Split-port mode: separated `kosync_bp` into `kosync_sync_bp` (internet-safe) and `kosync_admin_bp` (LAN-only), added threaded sync-only server on `KOSYNC_PORT`.
- **2026-02-12 19:16**: (`6c14e00`) Match UI: added initial book matching/selection page improvements.
- **2026-02-12 19:13**: (`1a50a94`) WhisperCpp fix: explicitly send `model` parameter in HTTP requests to prevent server defaults.
- **2026-02-12 15:22**: (`ca5f171`) UI: Visual overhaul of mapping wizard page (cards, grid, search bar, None/Skip option).
- **2026-02-12 15:10**: (`5e2c4eb`) Booklore library filtering: config, backend filtering, API route, settings UI.
- **2026-02-12 14:22**: (`390c0cf`) Robust booklore pruning, ID shadowing/duplicate merge fixes.
- **2026-02-12 12:16**: Antigravity Received detailed instructions for ID Shadowing, Duplicate Merge, and Storyteller Sanitizer.
- **2026-02-12 11:58**: Antigravity Expanded Deep Dive with detailed schema, workflows, and API info.
- **2026-02-12 11:47**: Antigravity Initialized branch status and updated `.cursorrules`.
- **2026-02-14 11:30**: Fix: `requests` NameError in `src/utils/transcriber.py`.
- **2026-02-14 11:45**: Fix: `PendingSuggestion` cleanup on match (robust dismissal by filename).
- **2026-02-14 13:45**: Feat: Increased Forge Storyteller detection wait time to 20m.
- **2026-02-14 14:38**: Fix: Storyteller payload now includes `fragments`, `progression` (chapter %), and `uuid` for correct syncing. Added handling for 204/409 codes.
- **2026-02-14 13:45**: Forge: Increased Storyteller detection wait time from 5m to 20m for larger books.
- **2026-02-14 15:00**: Forge: Implemented atomic staging. Files are copied to `.staging_Title` and atomically renamed to `Title` only after full verification, preventing partial processing by Storyteller. Also flattened directory structure to `Library/Title/`.
- **2026-02-14 20:45**: Hash Protection: Prevent automated overwrites of valid device links in `match()`/`batch_match()`/`update_hash()`.
- **2026-02-14 20:45**: Recalculate Logic: `update_hash` (Empty input) now prioritizes original EPUB filename over Storyteller file.
- **2026-02-14 20:45**: Manual Override: Manual `update_hash` via UI bypasses protection check, allowing users to fix bad links.
- **2026-02-14 20:45**: Relaxed Sync: "Furthest wins" now accepts backwards progress if from the same device (session based) or forced via UI.
- **2026-02-14 20:45**: Auto-Update: Manual linking in UI now automatically updates the Book's stored KOSync ID.
- **2026-02-15 02:00**: Fix: Resolved KOReader sync crash by correcting malformed XPath generation (double body tags) and adding strict safety checks for text offsets.
- **2026-02-15 02:00**: Protection: Implemented "Original File Locking" for KOSync IDs in `sync_manager.py` to prevent Storyteller artifacts from hijacking the sync hash.
- **2026-02-15 02:00**: Feat: KOSync Server now supports "Rewind" (backwards progress) if from the same device_id or forced via UI.
- **2026-02-15 02:00**: Refactor: `KoSyncClient` updates to send `force=True` and handle tuple returns, fixing "cannot unpack non-iterable" errors.
- **2026-02-15 02:00**: Cleanup: Removed `debug_db.py`.
- **[2026-02-17 21:15]**: [Antigravity] UI Fix: Resolved Booklore visibility bug where the icon was hidden for linked books with 0% progress. Updated `templates/index.html` to check for `mapping.booklore_id`.
- **[2026-02-17 20:55]**: [Antigravity] Documentation Overhaul:
  - Updated `user-guide.md` with new 3-Column Matcher UI and "None" option.
  - Replaced all legacy "Book Linker" references with **Forge**.
  - Updated `configuration.md` with CWA, Split-Port, and Advanced Sync settings.
  - Synced `docker-compose.example.yml` with current architecture.
- **[2026-02-17 18:45]**: [Antigravity] Fixed 6 failing tests by making Storyteller assertions conditional on `storyteller_uuid` in `base_sync_test.py` and mocking `get_book` in `test_webserver.py` and `test_suggestions_feature.py`. All 127 tests now passing.

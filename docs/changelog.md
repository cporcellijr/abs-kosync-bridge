# Changelog

For the full history of changes, please refer to the **[GitHub Releases](https://github.com/cporcellijr/abs-kosync-bridge/releases)** page.

---

## [6.3.4] - 2026-03-04

### Enhancements

- Added a dedicated **Library Suggestions** workspace at `/suggestions` with a split review + queue layout.
- Added asynchronous background scan execution with progress polling.
- Added persisted incremental scan cache (`/data/suggestions_scan_cache.json`) so routine scans can reuse prior work.
- Added an explicit **Full Refresh** action to force a complete rescan when needed.

### Fixes

- Fixed suggestion-scan completion failures caused by oversized cookie-backed Flask sessions by moving large payloads to server-side state.
- Reduced heavy ABS/Booklore scan traffic by loading ebook candidates once per scan and fuzzy matching in memory, rather than searching external providers per audiobook.
- Updated scan workload behavior to use Booklore `get_all_books()` for empty-query suggestions scans.

### Maintenance

- Kept suggestions scan logic centralized in `src/services/suggestions_service.py` for cleaner route handling.

---

## [6.3.3] - 2026-02-27

### Enhancements

- Storyteller forced-alignment transcript JSON is now a top-priority transcript source (before SMIL and Whisper).
- Added optional **Storyteller Assets Path** support (`STORYTELLER_ASSETS_DIR`) for ingesting files from `{root}/assets/{title}/transcriptions`.
- Added storyteller-native direct alignment map generation from `wordTimeline`.
- Added direct timestamp-to-EPUB locator resolution for Storyteller transcript books (bypasses fuzzy-search lookup path).
- Added a Settings maintenance action to bulk backfill Storyteller transcripts and regenerate alignment maps for existing Storyteller-linked books.

### Fixes

- Accepted both `00000-xxxxx.json` and `00001-xxxxx.json` Storyteller chapter filename prefixes.
- Added chapter format validation guardrails so incompatible JSON files are skipped cleanly during ingest/backfill.

---

## [6.3.0] - 2026-02-18

### 🚀 Features

- **Tri-Link Architecture**: Maintain a three-way link between ABS audiobook, KOReader ebook, and Storyteller entries.
- **Auto-Forge Pipeline**: Automated downloading, staging, and hand-off to Storyteller for processing. Triggered from the Matcher — automatically creates the sync mapping after Storyteller finishes.
- **Hardcover.app Audiobook Support**: Link specific editions and sync listening progress (in seconds).
- **Booklore & CWA (OPDS) Integration**: Fetch ebooks from Booklore and OPDS sources.
- **Split-Port Security Mode**: Run sync and admin UI on separate ports.
- **New Transcription Providers**: Support for Whisper.cpp Server, Deepgram API, and CUDA GPU acceleration.
- **Progress Suggestions**: Smart auto-discovery and suggestions for potential matches.
- **Telegram Notifications**: Send log alerts to a Telegram chat at a configurable severity level.
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

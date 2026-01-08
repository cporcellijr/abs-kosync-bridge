# ABS-KoSync Enhanced

> Multi-platform book synchronization system that keeps your reading progress in sync across audiobooks and ebooks.

Enhanced fork of [abs-kosync-bridge](https://github.com/00jlich/abs-kosync-bridge) with five-way sync, web UI, and automated workflows.

---

## 🎉 Release-Candidate Available for Testing!

**Thanks to [@giejay](https://github.com/giejay)**, the `Release-candidate` branch is back and ready for testing!

| Branch | Status | Recommendation |
|--------|--------|----------------|
| `main` | Stable (Release 5.9) | Production use |
| `Release-candidate` | Testing | Early adopters & feedback welcome |

👉 **Want to help test?** Check out the `Release-candidate` branch and report any issues!

---

## What It Does

ABS-KoSync Enhanced synchronizes your reading/listening progress across multiple platforms:

| Platform | Type | Direction |
|----------|------|-----------|
| **Audiobookshelf** | Audiobooks | ↔ Read/Write |
| **KOReader/KOSync** | Ebooks | ↔ Read/Write |
| **Storyteller** | Enhanced Reader | ↔ Read/Write |
| **Booklore** | Ebook Management | ↔ Read/Write |
| **Hardcover.app** | Book Tracking | → Write Only |

**How it works:** When you listen to an audiobook, the system uses AI transcription to find your exact position in the matching ebook. When you read an ebook, it finds the corresponding timestamp in the audiobook. All platforms stay in sync automatically.

---

## Features

### Core Sync Engine
- **Five-way synchronization** between ABS, KOSync, Storyteller, Booklore, and Hardcover
- **AI-powered transcription** using Whisper for precise audio-to-text matching
- **Smart conflict resolution** - "furthest progress wins" with anti-regression protection
- **Rich position data** - syncs XPath, CSS selectors, and CFI for precise ebook positioning
- **Resumable transcription** - interrupted jobs resume where they left off

### Web Management Interface
- **Dashboard** with cover art and real-time progress across all platforms
- **Single match** - manually link audiobooks to ebooks
- **Batch matching** - queue multiple books for processing
- **Suggestions** - auto-discovered matches from your reading activity
- **Book Linker** - automated Storyteller readaloud workflow

### Automation
- **Background sync daemon** with configurable intervals
- **Auto-retry** for failed transcription jobs
- **File monitoring** for Storyteller processing workflow
- **Auto-organization** into ABS collections and Booklore shelves
- **Hardcover integration** with automatic started/finished dates

---

## Quick Start

### 1. Create Directory Structure

```bash
mkdir abs-kosync-enhanced
cd abs-kosync-enhanced
mkdir data
```

### 2. Create docker-compose.yml

```yaml
services:
  abs-kosync-enhanced:
    image: ghcr.io/your-username/abs-kosync-enhanced:latest
    container_name: abs-kosync
    restart: unless-stopped
    
    environment:
      # === REQUIRED ===
      - ABS_SERVER=https://your-audiobookshelf.com
      - ABS_KEY=your_abs_api_token
      - ABS_LIBRARY_ID=your_library_id
      
      # === OPTIONAL: KOSync (for KOReader sync) ===
      - KOSYNC_SERVER=https://your-calibre.com/api/koreader
      - KOSYNC_USER=your_username
      - KOSYNC_KEY=your_password
      
    volumes:
      - ./data:/data
      - /path/to/your/ebooks:/books
    
    ports:
      - "8080:5757"
```

### 3. Start the Container

```bash
docker compose up -d
```

### 4. Access Web UI

Open `http://localhost:8080` in your browser.

### 5. Create Your First Mapping

1. Click **"Add Book"**
2. Search for a title
3. Select the audiobook and matching ebook
4. Click **"Create Mapping"**

The system will transcribe the audiobook (this takes time on first run) and begin syncing.

---

## Environment Variables Reference

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `ABS_SERVER` | Audiobookshelf server URL | `https://abs.example.com` |
| `ABS_KEY` | ABS API token (Settings → Users → API Token) | `eyJ...` |
| `ABS_LIBRARY_ID` | Library ID from URL (`/library/THIS_PART`) | `lib_abc123` |

### KOSync Integration (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `KOSYNC_SERVER` | - | KOSync server URL |
| `KOSYNC_USER` | - | KOSync username |
| `KOSYNC_KEY` | - | KOSync password |
| `KOSYNC_HASH_METHOD` | `content` | Hash method: `content` (KOReader default) or `filename` |

### Storyteller Integration (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `STORYTELLER_API_URL` | - | Storyteller REST API URL (recommended) |
| `STORYTELLER_USER` | - | Storyteller username |
| `STORYTELLER_PASSWORD` | - | Storyteller password |
| `STORYTELLER_DB_PATH` | - | SQLite path (legacy fallback, not recommended) |

> **Note:** REST API mode prevents the Storyteller mobile app from overwriting synced positions. Use `host.docker.internal` to reach host machine from container.

### Booklore Integration (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOKLORE_SERVER` | - | Booklore server URL |
| `BOOKLORE_USER` | - | Booklore username |
| `BOOKLORE_PASSWORD` | - | Booklore password |
| `BOOKLORE_SHELF_NAME` | `Kobo` | Auto-add synced books to this shelf |

### Hardcover Integration (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `HARDCOVER_TOKEN` | - | API token from [hardcover.app/account/api](https://hardcover.app/account/api) |

### Sync Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNC_PERIOD_MINS` | `5` | How often to check for progress changes (minutes) |
| `SYNC_DELTA_ABS_SECONDS` | `60` | Minimum audiobook change (seconds) to trigger sync |
| `SYNC_DELTA_KOSYNC_PERCENT` | `1` | Minimum ebook change (percentage 0-100) to trigger sync |
| `SYNC_DELTA_KOSYNC_WORDS` | `400` | Minimum word count change to trigger sync |
| `FUZZY_MATCH_THRESHOLD` | `80` | Text matching accuracy (0-100, higher = stricter) |
| `TRANSCRIPT_MATCH_THRESHOLD` | `80` | Transcript matching threshold (falls back to `FUZZY_MATCH_THRESHOLD`) |
| `ABS_PROGRESS_OFFSET_SECONDS` | `0` | Offset applied to ABS progress (e.g., `-60` to sync 1 minute back) |
| `KOSYNC_USE_PERCENTAGE_FROM_SERVER` | `false` | Use KOSync server percentage directly instead of calculating from text matching |

### Transcription

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_MODEL` | `tiny` | Whisper model size: `tiny`, `base`, `small`, `medium`, `large` |

> **Note:** Larger models are more accurate but require more RAM and time. `tiny` works well for most books.

### Job Processing

| Variable | Default | Description |
|----------|---------|-------------|
| `JOB_MAX_RETRIES` | `5` | Maximum retry attempts for failed transcription jobs |
| `JOB_RETRY_DELAY_MINS` | `15` | Minutes to wait between retry attempts |

### Book Linker Workflow (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `LINKER_BOOKS_DIR` | `/linker_books` | Source ebooks for Storyteller workflow |
| `PROCESSING_DIR` | `/processing` | Storyteller processing folder |
| `STORYTELLER_INGEST_DIR` | `/linker_books` | Storyteller library (final destination) |
| `AUDIOBOOKS_DIR` | `/audiobooks` | Audiobook files location |
| `MONITOR_INTERVAL` | `3600` | Seconds between readaloud file checks |

### Auto-Organization (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_COLLECTION_NAME` | `Synced with KOReader` | Auto-add matched books to this ABS collection |

### General

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | - | Timezone (e.g., `America/New_York`) |
| `LOG_LEVEL` | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `EBOOK_CACHE_SIZE` | `3` | Number of parsed EPUBs to keep in memory |

---

## Volume Mounts

### Required

| Mount | Container Path | Description |
|-------|----------------|-------------|
| App data | `/data` | Database, transcripts, state files |
| Ebook library | `/books` | Your EPUB files for sync matching |

### Optional (Book Linker)

| Mount | Container Path | Description |
|-------|----------------|-------------|
| Source ebooks | `/linker_books` | EPUBs for Storyteller workflow |
| Audiobooks | `/audiobooks` | Audio files (for direct copy) |
| Processing | `/processing` | Storyteller temp folder |
| Storyteller library | `/storyteller_ingest` | Final destination |

### Optional (Storyteller SQLite)

| Mount | Container Path | Description |
|-------|----------------|-------------|
| Storyteller data | `/storyteller_data` | Contains `storyteller.db` |

---

## Complete Example Configuration

```yaml
services:
  abs-kosync-enhanced:
    image: ghcr.io/your-username/abs-kosync-enhanced:latest
    container_name: abs-kosync
    restart: unless-stopped
    
    environment:
      # General
      - TZ=America/New_York
      - LOG_LEVEL=INFO
      
      # === REQUIRED: Audiobookshelf ===
      - ABS_SERVER=https://audiobookshelf.example.com
      - ABS_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
      - ABS_LIBRARY_ID=lib_abc123def456
      - ABS_COLLECTION_NAME=Synced with KOReader
      
      # === OPTIONAL: KOSync ===
      - KOSYNC_SERVER=https://calibre.example.com/api/koreader
      - KOSYNC_USER=myuser
      - KOSYNC_KEY=mypassword
      - KOSYNC_HASH_METHOD=content
      
      # === OPTIONAL: Storyteller (REST API - Recommended) ===
      - STORYTELLER_API_URL=http://host.docker.internal:8001
      - STORYTELLER_USER=admin
      - STORYTELLER_PASSWORD=secretpassword
      
      # === OPTIONAL: Booklore ===
      - BOOKLORE_SERVER=https://booklore.example.com
      - BOOKLORE_USER=myuser
      - BOOKLORE_PASSWORD=mypassword
      - BOOKLORE_SHELF_NAME=Kobo
      
      # === OPTIONAL: Hardcover ===
      - HARDCOVER_TOKEN=hc_abc123...
      
      # === Sync Tuning ===
      - SYNC_PERIOD_MINS=5
      - SYNC_DELTA_ABS_SECONDS=60
      - SYNC_DELTA_KOSYNC_PERCENT=0.5
      - SYNC_DELTA_KOSYNC_WORDS=400
      - FUZZY_MATCH_THRESHOLD=85
      - WHISPER_MODEL=base
      # - ABS_PROGRESS_OFFSET_SECONDS=-60  # Uncomment to sync 1 minute back
      # - KOSYNC_USE_PERCENTAGE_FROM_SERVER=false
      
    volumes:
      # Required
      - ./data:/data
      - /mnt/books/ebooks:/books
      
      # Optional: Book Linker workflow
      - /mnt/books/ebooks:/linker_books
      - /mnt/audiobooks:/audiobooks
      - /mnt/storyteller/processing:/processing
      - /mnt/storyteller/library:/storyteller_ingest
      
    ports:
      - "8080:5757"
```

---

## Web Interface Guide

### Dashboard (`/`)

The main dashboard shows all mapped books with:
- Cover art from Audiobookshelf
- Progress from all connected platforms (ABS, KOSync, Storyteller, Booklore)
- Sync status and last sync time
- Quick actions: Clear progress, Delete mapping

**Sorting:** Click column headers to sort by title, progress, status, or last sync time.

### Add Book (`/match`)

Create a single audiobook-to-ebook mapping:
1. Search by title, author, or filename
2. Select the audiobook from the grid
3. Select the matching ebook from the dropdown
4. Click "Create Mapping"

### Batch Match (`/batch-match`)

Queue multiple mappings at once:
1. Search and add pairs to the queue
2. Review the queue in the sidebar
3. Click "Process All" to create all mappings

### Suggestions (`/suggestions`)

Auto-discovered potential matches:
- Based on fuzzy title matching between your active audiobooks/ebooks
- Accept to create a mapping, or Dismiss to ignore
- Confidence score shows match quality

### Book Linker (`/book-linker`)

Automated Storyteller readaloud workflow:
1. Search for a book
2. Select ebook + audiobook files
3. Click "Process Selected"
4. Files are copied to Storyteller's processing folder
5. Monitor automatically detects completed readaloud files
6. Moves them to the ingest folder and cleans up

---

## How Sync Works

### The Sync Cycle

Every 5 minutes (configurable), the daemon:

1. **Fetches progress** from all connected platforms
2. **Determines the leader** - whichever platform has the furthest progress
3. **Converts position** using text matching:
   - Audio → Text: Get transcript at timestamp, find matching text in ebook
   - Text → Audio: Get ebook text at position, find matching timestamp in transcript
4. **Updates all platforms** with the converted position
5. **Saves state** to prevent regression

### Anti-Regression Protection

The system prevents accidental progress loss:
- Won't sync backwards if leader reports 0%
- Tracks last known state per book
- Requires minimum delta change to trigger sync

### Text Matching

Position conversion uses fuzzy text matching:
1. Extract ~800 characters around the position
2. Search for matching text using token-based fuzzy matching
3. Use hint percentage to narrow search window
4. Return position if match score exceeds threshold

---

## Troubleshooting

### Transcription Takes Forever

- **Large audiobooks** are split into chunks automatically
- **First transcription** downloads audio files (can be slow)
- **Check logs:** `docker compose logs -f`
- **Adjust model:** Use `WHISPER_MODEL=tiny` for faster (less accurate) results

### Sync Not Working

1. **Check connectivity:**
   ```bash
   docker compose logs | grep "Connected"
   ```
   You should see ✅ for each configured service.

2. **Verify API keys:**
   - ABS: Check if cover images load in web UI
   - KOSync: Try manual sync in KOReader app

3. **Check mapping status:**
   - `pending`: Waiting for transcription
   - `processing`: Currently transcribing
   - `active`: Ready to sync
   - `failed_retry_later`: Will retry automatically

### Progress Regression

If progress keeps going backwards:
1. Check if multiple devices are syncing to the same platforms
2. Verify Storyteller is using API mode (not SQLite)
3. Clear progress and restart from one platform

### Book Not Found

For ebook matching to work, you need either:
- **Booklore integration** configured (API-based, no volume mount needed)
- **`/books` volume** mounted with your EPUBs

### Memory Issues

Large libraries can cause memory pressure:
- Reduce `EBOOK_CACHE_SIZE` (default: 3)
- Use `WHISPER_MODEL=tiny`
- Ensure adequate container memory limits

---

## Building from Source

```bash
git clone https://github.com/your-username/abs-kosync-enhanced.git
cd abs-kosync-enhanced

# Build
docker build -t abs-kosync-enhanced:latest .

# Run
docker compose up -d
```

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard |
| `/match` | GET/POST | Single match interface |
| `/batch-match` | GET/POST | Batch matching |
| `/suggestions` | GET | Auto-discovered matches |
| `/suggestions/accept/<key>` | POST | Accept a suggestion |
| `/suggestions/dismiss/<key>` | POST | Dismiss a suggestion |
| `/book-linker` | GET/POST | Book Linker workflow |
| `/delete/<abs_id>` | POST | Delete a mapping |
| `/clear-progress/<abs_id>` | POST | Reset progress to 0% |
| `/api/status` | GET | JSON status of all mappings |
| `/view_log` | GET | View application logs |

---

## Data Files

All persistent data is stored in `/data`:

| File | Description |
|------|-------------|
| `mapping_db.json` | Book mappings and configuration |
| `last_state.json` | Last known sync state per book |
| `suggestions.json` | Auto-discovered match suggestions |
| `transcripts/` | Whisper transcription output (JSON) |
| `audio_cache/` | Temporary audio file downloads |
| `epub_cache/` | Downloaded EPUBs from Booklore |
| `logs/` | Application logs |

---

## Companion Apps

For best results with Storyteller, we recommend:
- **[Silveran Reader](https://github.com/kyonifer/silveran-reader)** - Available for Apple devices, provides consistent sync behavior

---

## Credits

This project is an enhanced fork of [abs-kosync-bridge](https://github.com/00jlich/abs-kosync-bridge) by 00jlich.

**Enhancements include:**
- Five-way sync (Storyteller, Booklore, Hardcover)
- Web management interface
- Book Linker automation
- Batch matching
- Rich locator support (XPath, CSS, CFI)
- Resumable transcription jobs
- Anti-regression protection

---

## License

MIT License - see [LICENSE](LICENSE) file.

---

## Support

Found a bug? Have a feature request?

Please open an issue with:
- Your `docker-compose.yml` (remove sensitive values)
- Container logs: `docker compose logs`
- Steps to reproduce

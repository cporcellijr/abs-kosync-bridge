# ABS-KOSync Bridge (Enhanced)

Enhanced version of [abs-kosync-bridge](https://github.com/jLichti/abs-kosync-bridge) with three-way sync and Book Linker features.

[![Docker](https://img.shields.io/badge/docker-build-blue.svg)](https://github.com/cporcellijr/abs-kosync-bridge)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## 🎯 What This Does

Seamlessly sync your reading progress between:
- **Audiobookshelf** (audiobooks) ↔
- **KOReader/KOSync** (ebooks on Kobo/Kindle) ↔
- **Storyteller** (enhanced web reader)

Start listening in your car, continue reading on your Kobo, pick up on Storyteller - your progress stays in sync!

## ✨ New Features in This Fork

### Three-Way Sync
- **Storyteller Integration:** Full three-way progress sync
- **Anti-Regression:** Prevents accidental backwards sync
- **Conflict Resolution:** Smart handling when multiple sources change
- **Configurable Thresholds:** Fine-tune sync sensitivity

### Book Linker Workflow
- **Automated Processing:** Prepare books for Storyteller with one click
- **Smart Monitoring:** Detects completed processing and cleans up automatically
- **Safety Checks:** Prevents interference with active processing
- **Folder Preservation:** Maintains organized library structure

### Web Interface
- **Flask UI:** Manage all mappings through web interface
- **Real-Time Progress:** See sync status across all three systems
- **Batch Operations:** Match multiple books at once
- **Search-on-Demand:** Fast page loads, only fetches data when needed

### Enhanced Management
- **Complete Cleanup:** Delete removes mappings, state, and transcripts
- **Collection Auto-Add:** Automatically adds books to ABS collections
- **Booklore Integration:** Optional shelf management
- **Flexible Configuration:** Environment variable-based setup

## 📸 Screenshots
   
   ### Main Dashboard - Three-Way Progress Sync
  <img width="1451" height="651" alt="image" src="https://github.com/user-attachments/assets/7480abb9-2800-483c-905c-33293e8a560b" />

   
   ### Book Linker Workflow
<img width="1451" height="712" alt="image" src="https://github.com/user-attachments/assets/144386b3-3a05-4dc6-aea5-84b77176d13f" />

 
   ### Book Match
<img width="1270" height="675" alt="image" src="https://github.com/user-attachments/assets/7d74d7f7-be10-4ed1-86a7-716c1092ca07" />

<img width="1670" height="705" alt="image" src="https://github.com/user-attachments/assets/7d81f081-afc4-4418-bcea-ec15a99d36ae" />

   
## 📦 Quick Start

### 1. Clone the Repository
```bash
git clone https://github.com/cporcellijr/abs-kosync-bridge.git
cd abs-kosync-bridge
```

### 2. Configure
```bash
cp docker-compose.example.yml docker-compose.yml
nano docker-compose.yml  # Edit with your settings
```

### 3. Run
```bash
docker compose up -d
```

### 4. Access Web UI
Open http://localhost:8080

## 📖 Full Documentation

- **[Quick Start Guide](QUICKSTART.md)** - Get running in 10 minutes
- **[Configuration Reference](docker-compose.example.yml)** - All environment variables explained
- **[Changelog](CHANGELOG.md)** - What's new in this version

## 🔧 Configuration

### Required Settings
```yaml
environment:
  # Audiobookshelf
  - ABS_SERVER=https://your-abs-server.com
  - ABS_KEY=your_api_key
  
  # KOSync
  - KOSYNC_SERVER=https://your-server.com/api/koreader
  - KOSYNC_USER=username
  - KOSYNC_KEY=password
```

### Optional Features
```yaml
  # Storyteller (three-way sync)
  - STORYTELLER_DB_PATH=/storyteller_data/storyteller.db
  - STORYTELLER_USER_ID=your_user_id
  
  # Book Linker (automated workflow)
  - MONITOR_INTERVAL=3600
  - STORYTELLER_INGEST_DIR=/path/to/library
  
  # Integrations
  - ABS_COLLECTION_NAME=Synced with KOReader
  - BOOKLORE_SHELF_NAME=Kobo
```

## 🎯 Use Cases

### Original Two-Way Sync (ABS ↔ KOSync)
Perfect if you:
- Listen to audiobooks in Audiobookshelf
- Read ebooks on Kobo or Kindle with KOReader
- Want progress to sync between audio and ebook versions

### Enhanced Three-Way Sync (+ Storyteller)
Perfect if you:
- Also use Storyteller for enhanced web reading
- Want seamless switching between listening, e-reader, and web
- Need progress sync across all three platforms

### Book Linker Workflow
Perfect if you:
- Use Storyteller's readaloud feature
- Want automated processing of ebook + audiobook pairs
- Need organized management of processed books

## 🔄 How It Works

### Sync Flow
```
ABS Progress Changed → Transcribe audio → Find matching text in ebook → Update KOSync & Storyteller
KOSync Changed → Find text in ebook → Find matching audio → Update ABS & Storyteller  
Storyteller Changed → Find text in ebook → Find matching audio → Update ABS & KOSync
```

### Book Linker Flow
```
Select ebook + audiobook → Copy to processing folder → Storyteller processes →
Monitor detects completion → Move to library → Clean up originals
```


## ⚠️ Known Limitations

### Storyteller: Book Must Be Opened First

**Important:** For Storyteller sync to work, you must open each book in Storyteller at least once before syncing will function.

**Why?** The sync system updates existing position entries in Storyteller's database. Storyteller only creates a position entry when you first open a book in its UI. Without this initial entry, there's nothing for the sync to update.

**Workaround:**
1. After processing a book with Book Linker (or manually adding it to Storyteller)
2. Open the book once in Storyteller's web interface
3. You can close it immediately - this creates the required database entry
4. Sync will now work for that book

**Affected scenarios:**
- New books processed via Book Linker
- Books added directly to Storyteller's library folder
- Any book not yet opened in Storyteller's UI

*Future versions may add automatic position entry creation to eliminate this requirement.*

### Other Limitations

- **Transcription RAM:** Long audiobooks (3+ hours per file) may require significant RAM during initial transcription. Files are automatically split into 45-minute chunks to mitigate this.
- **Fuzzy Matching:** Text matching between audio transcripts and ebooks works best with professionally narrated audiobooks that closely follow the text.
- **KOReader Hash Method:** Ensure your `KOSYNC_HASH_METHOD` matches your KOReader settings (usually `content`).

## 🙏 Credits

This is an enhanced fork of [abs-kosync-bridge](https://github.com/jLichti/abs-kosync-bridge) by [jLichti](https://github.com/jLichti).

**Original features:**
- Two-way sync between Audiobookshelf and KOSync
- Audio transcription with Whisper AI
- Fuzzy text matching

**Enhancements by [cporcellijr](https://github.com/cporcellijr):**
- Storyteller DB integration for three-way sync
- Book Linker workflow automation
- Flask web interface
- Batch operations and enhanced UX

## 📝 License

MIT License - See [LICENSE](LICENSE) file

## 🐛 Issues & Contributions

Found a bug? Have a feature request?
- Open an issue on [GitHub Issues](https://github.com/cporcellijr/abs-kosync-bridge/issues)
- Check the original repo for upstream issues: [jLichti/abs-kosync-bridge](https://github.com/jLichti/abs-kosync-bridge)

## 🔗 Related Projects

- [Audiobookshelf](https://github.com/advplyr/audiobookshelf) - Self-hosted audiobook server
- [KOReader](https://github.com/koreader/koreader) - Ebook reader for E Ink devices
- [Storyteller](https://github.com/smoores-dev/storyteller) - Enhanced web-based ebook reader

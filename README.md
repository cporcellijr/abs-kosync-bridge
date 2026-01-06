
For most consistent sync with Storyteller recommend using https://github.com/kyonifer/silveran-reader Available for Apple devices.


**Important: Current Branch Status**

- The `main` branch of the project (no longer under active development).
- Active development and user testing are happening on the **`Release-candidate`** branch.
- This branch represents a **full rework** of the application with major changes and new features.
- ⚠️ `Release-candidate` is currently **in testing**. 

👉 **If you want the latest version for testing or development**, check out the `Release-candidate` branch.

Contributions and pull requests should be based on `Release-candidate`.

Once testing is complete and the rework is stabilized, `Release-candidate` will become the new `main`.

# ABS-KoSync Enhanced

> Enhanced fork of [abs-kosync-bridge](https://github.com/00jlich/abs-kosync-bridge) with three-way sync, web UI, and automated workflows.

## 🌟 Features

### Core Sync Capabilities
- **Three-way synchronization** between:
  - 📱 Audiobookshelf (ABS) - audiobook progress
  - 📖 KOReader/KOSync - ebook progress  
  - 📚 Storyteller - enhanced reading app
- **AI-powered transcription** for precise audio-to-text matching
- **Smart progress tracking** with anti-regression safeguards
- **Configurable sync thresholds** to prevent loops

### Web Management Interface
- 🎨 **Modern web UI** for easy management
- 📋 **Batch matching queue** for multiple books
- 🔗 **Book Linker workflow** for Storyteller integration
- 📊 **Real-time progress monitoring** across all platforms
- 🎯 **Single & bulk matching** interfaces

### Automation Features
- ⚙️ **Automated file monitoring** for Storyteller workflows
- 📦 **Auto-organization** into ABS collections
- 🏷️ **Booklore shelf integration** (optional)
- 🔄 **Background sync daemon** with configurable intervals

---

## 🚀 Quick Start

### Prerequisites
- Docker & Docker Compose
- Audiobookshelf server (required)
- KOSync/Calibre server (required)
- Storyteller app (optional)

### Basic Setup

1. **Create your directory structure:**
```bash
mkdir abs-kosync-enhanced
cd abs-kosync-enhanced
```

2. **Download the example compose file:**
```bash
# Copy docker-compose.example.yml to docker-compose.yml
# Edit with your server details
```

3. **Configure your environment variables:**
Edit `docker-compose.yml` with your server URLs, API keys, and paths.

4. **Start the container:**
```bash
docker compose up -d
```

5. **Access the web interface:**
Open `http://localhost:8080` in your browser

---

## ⚙️ Configuration

### Required Environment Variables

```yaml
# Audiobookshelf Configuration
ABS_SERVER=https://your-audiobookshelf-server.com
ABS_KEY=your_abs_api_key
ABS_LIBRARY_ID=your_library_id

# KoSync Configuration  
KOSYNC_SERVER=https://your-calibre-server.com/api/koreader
KOSYNC_USER=your_username
KOSYNC_KEY=your_password
KOSYNC_HASH_METHOD=content
```

### Optional Features

#### Storyteller Integration
```yaml
STORYTELLER_DB_PATH=/storyteller_data/storyteller.db
STORYTELLER_USER_ID=your_user_id_here
```

#### Book Linker Workflow
```yaml
MONITOR_INTERVAL=3600  # Check for readaloud files every hour
LINKER_BOOKS_DIR=/linker_books
PROCESSING_DIR=/processing
STORYTELLER_INGEST_DIR=/storyteller_ingest
AUDIOBOOKS_DIR=/audiobooks
```

#### ABS Collection Auto-Add
```yaml
ABS_COLLECTION_NAME=Synced with KOReader
```

#### Booklore Integration
```yaml
BOOKLORE_SERVER=https://your-calibre-server.com
BOOKLORE_USER=your_username
BOOKLORE_PASSWORD=your_password
BOOKLORE_SHELF_NAME=Kobo
```

#### Sync Behavior Tuning
```yaml
SYNC_PERIOD_MINS=5              # How often to check for changes
SYNC_DELTA_ABS_SECONDS=60       # Min seconds change to trigger sync
SYNC_DELTA_KOSYNC_PERCENT=1     # Min percentage change to trigger sync
SYNC_DELTA_KOSYNC_WORDS=400     # Min word count change to trigger sync
FUZZY_MATCH_THRESHOLD=80        # Text matching accuracy (0-100)
```

---

## 📁 Volume Mounts

### Required Volumes

```yaml
volumes:
  # App data (database, transcripts, state)
  - ./data:/data
  
  # Your main ebook library (for sync matching)
  - /path/to/ebooks:/books
```

### Optional Volumes (Book Linker)

```yaml
volumes:
  # Source ebooks for Storyteller workflow
  - /path/to/source/ebooks:/linker_books
  
  # Audiobook files
  - /path/to/audiobooks:/audiobooks
  
  # Storyteller processing folder
  - /path/to/storyteller/temp:/processing
  
  # Storyteller library (final destination)
  - /path/to/storyteller/library:/storyteller_ingest
```

### Optional Volumes (Storyteller)

```yaml
volumes:
  # Storyteller database
  - /path/to/storyteller/data:/storyteller_data
```

---

## 📖 Usage Guide

### Creating Book Mappings

#### Option 1: Single Match
1. Click **"Single Match"** in the web UI
2. Select an audiobook from your ABS library
3. Select the matching ebook
4. Click **"Create Mapping"**

#### Option 2: Batch Match
1. Click **"Batch Match"**
2. Add multiple audiobook/ebook pairs to the queue
3. Click **"Process All"** when ready

### Book Linker Workflow

1. Click **"Book Linker"**
2. Search for a book title
3. Select ebooks and audiobooks
4. Click **"Process Selected"**
5. Files are copied to Storyteller's processing folder
6. After Storyteller processes them, the monitor automatically:
   - Moves readaloud files to ingest folder
   - Cleans up temporary files

### Monitoring Sync Status

The main dashboard shows:
- 🎧 Current audiobook progress (time)
- 📖 Current ebook progress (percentage)
- 📚 Current Storyteller progress (percentage)
- 🔄 Last sync time
- 📊 Unified progress bar (furthest position)

---

## 🔧 Troubleshooting

### Sync Not Working

1. **Check connectivity:**
   - View container logs: `docker compose logs -f`
   - Look for connection errors on startup

2. **Verify API keys:**
   - Test ABS connection: Check if cover images load
   - Test KOSync: Try manual sync in KOReader

3. **Check file permissions:**
   - Ensure container can read ebook folders
   - Verify write access to `/data` volume

### Book Linker Issues

1. **Files not being processed:**
   - Check monitor interval (default 1 hour)
   - Manually trigger: Click "Check Now" in Book Linker

2. **Storyteller not finding files:**
   - Verify folder paths in Storyteller settings
   - Check volume mounts match Storyteller's expectations

### Progress Regression

The system includes anti-regression protection. If you intentionally restart a book:
1. Manually reset progress in all systems (ABS, KOReader, Storyteller)
2. Or delete and recreate the mapping

---

## 🏗️ Building from Source

```bash
# Clone the repository
git clone https://github.com/yourusername/abs-kosync-enhanced.git
cd abs-kosync-enhanced

# Build the Docker image
docker build -t abs-kosync-enhanced:latest .

# Run with docker compose
docker compose up -d
```

---

## 🙏 Credits

This project is an enhanced fork of [abs-kosync-bridge](https://github.com/00jlich/abs-kosync-bridge) by 00jlich.

**Enhancements include:**
- Three-way sync with Storyteller
- Web management interface
- Book Linker automation workflow
- Batch matching capabilities
- Enhanced progress tracking
- Collection/shelf auto-organization

---

## 📄 License

[Same license as original project]

---

## 🐛 Issues & Contributions

Found a bug? Have a feature request? 

Please open an issue on GitHub with:
- Your docker-compose.yml (remove sensitive info)
- Container logs (`docker compose logs`)
- Steps to reproduce the issue

---

## 🔮 Roadmap

- [ ] Auto-detection of new audiobook activity
- [ ] Suggested book matching
- [ ] OPDS server support for ebook browsing
- [ ] Multi-user support
- [ ] Mobile-friendly UI
- [ ] Progress export/import

---

## ℹ️ FAQ

**Q: Can I use this without Storyteller?**  
A: Yes! Just don't configure Storyteller environment variables. The core ABS ↔ KOSync sync works independently.

**Q: Does this work with any KOSync server?**  
A: Yes, any KOSync-compatible server works (Calibre with KOSync plugin, dedicated KOSync servers, etc.)

**Q: Can I use the same ebook folder for both /books and /linker_books?**  
A: Yes, they can be the same folder. One is for matching, one is for the Linker workflow.

**Q: How much disk space do I need?**  
A: Transcripts can be large (several MB per audiobook). Budget at least 100MB per mapped audiobook for transcripts and cache.

**Q: Can I run multiple instances?**  
A: Not recommended. Multiple instances would conflict over progress updates. Use one instance per user.


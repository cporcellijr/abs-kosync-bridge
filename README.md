# ABS-KoSync Bridge

**The missing link between your ears and your eyes.**

Seamlessly sync your reading progress between **Audiobookshelf** (Audiobooks) and **KOReader/KoSync** (Ebooks). Start listening in the car, and pick up exactly where you left off on your Kindle or Kobo.

## 🧠 How It Works

This is not a simple "percentage matcher." Audiobooks and Ebooks have different structures, speeds, and layouts. 50% of a file rarely equals 50% of a recording.

**ABS-KoSync Bridge** uses AI and semantic analysis to create a true link:
1. Whisper AI Ingestion: The system downloads the audiobook and uses OpenAI's Faster-Whisper model to generate a precise timestamped transcript.
2. Context-Aware Matching:
    * **Audio -> Ebook:** It takes the spoken text at the current timestamp, expands the context to ~400 characters (to avoid matching short phrases like "He said"), and finds that exact block of text in your EPUB.
    * **Reverse XPath Generation:** Instead of sending a generic percentage, it calculates the exact DOM path (e.g., `/body/DocFragment[4]/div/p[22])` required by KOReader to navigate to that specific paragraph.
3. Loop Prevention: Configurable thresholds (time and percentage) ensure that rounding errors between platforms don't cause infinite sync loops.

## ✨ Key Features

- Smart Matching Strategies: Tries 4 levels of matching:
  1. Exact: 1:1 text match.
  2. Case-Insensitive: Ignores capitalization.
  3. Normalized: Strips punctuation and whitespace (handles "Smelting's" vs "Smeltings").
  4. Fuzzy Token: Uses `rapidfuzz` to match sentences with a confidence score (default >80%).
- Robust Caching: Audio files are downloaded to a local cache before processing begins. This prevents corruption if using network mounts (rclone/NFS) and saves hours of lost progress.
- Crash Recovery: If the container runs out of RAM during a massive transcription, it detects the crash on reboot and flags the job, preventing infinite boot loops.
- Resource Optimized: Tuned for low-memory environments (like Raspberry Pi) using greedy search (`beam_size=1`) and aggressive garbage collection.
- Non-Blocking Wizard: The `match` CLI command queues jobs instantly. You can close your terminal immediately, and the background daemon will handle the heavy lifting.
- KOReader Native Hashing: Supports the specific "Content Hash" (fastDigest) used by KOReader, ensuring matches even if you rename your files.

## 🚀 Deployment

The following options for deployment have been provided
1. Docker compose (Dockerhub)
2. Docker build (Local)
3. Full Stack example (ABS / KoSync)

### 1. Docker Compose (Dockerhub)

```yml
services:
  # ---------------------------------------------------------------------------
  # 1. The Bridge Service
  # ---------------------------------------------------------------------------
  abs-kosync:
    image: 00jlich/abs-kosync-bridge:latest
    container_name: abs_kosync
    restart: unless-stopped
    # depends_on:
    #  - audiobookshelf
    #  - kosync
    
    # CRITICAL: Machine Learning libraries need shared memory
    shm_size: '2gb'

    environment:
      - TZ=America/New_York
      # --- Server Connections ---
      - ABS_SERVER=http://audiobookshelf:80
      - ABS_KEY=your_abs_api_key_here
      - KOSYNC_SERVER=http://kosync:3000
      - KOSYNC_USER=admin
      - KOSYNC_KEY=your_kosync_password
      
      # --- Sync Logic ---
      - SYNC_PERIOD_MINS=5
      # Loop Prevention: Ignore small changes caused by rounding errors
      - SYNC_DELTA_ABS_SECONDS=60
      - SYNC_DELTA_KOSYNC_PERCENT=1
      
      # --- Matching Logic ---
      - FUZZY_MATCH_THRESHOLD=80
      - KOSYNC_HASH_METHOD=content  # Use 'content' for KOReader native hashing
      
    volumes:
      # Map the EXACT same folder structure used by your KOReader device if possible,
      # or just the root folder containing your EPUBs.
      - ./library:/books
      - ./bridge_data:/data
```

<details>
<summary> 2. Docker Build </summary>

#### Download the project
   `git clone [https://github.com/j-lich/abs-kosync-bridge.git](https://github.com/j-lich/abs-kosync-bridge.git)`
   
   `cd abs-kosync-bridge`

#### Configure docker-compose.yml
```yml
services:
  # ---------------------------------------------------------------------------
  # 1. The Bridge Service
  # ---------------------------------------------------------------------------
  abs-kosync:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: abs_kosync
    restart: unless-stopped
    # depends_on:
    #  - audiobookshelf
    #  - kosync
    
    # CRITICAL: Machine Learning libraries need shared memory
    shm_size: '2gb'

    environment:
      - TZ=America/New_York
      # --- Server Connections ---
      - ABS_SERVER=http://audiobookshelf:80
      - ABS_KEY=your_abs_api_key_here
      - KOSYNC_SERVER=http://kosync:3000
      - KOSYNC_USER=admin
      - KOSYNC_KEY=your_kosync_password
      
      # --- Sync Logic ---
      - SYNC_PERIOD_MINS=5
      # Loop Prevention: Ignore small changes caused by rounding errors
      - SYNC_DELTA_ABS_SECONDS=60
      - SYNC_DELTA_KOSYNC_PERCENT=1
      
      # --- Matching Logic ---
      - FUZZY_MATCH_THRESHOLD=80
      - KOSYNC_HASH_METHOD=content  # Use 'content' for KOReader native hashing
      
    volumes:
      # Map the EXACT same folder structure used by your KOReader device if possible,
      # or just the root folder containing your EPUBs.
      - ./library:/books
      - ./bridge_data:/data
```
</details>

<details>
<summary> 3. Docker Compose (Full Stack Example) </summary>

The included docker-compose.yml provides a full example stack.

```yml
services:
  # ---------------------------------------------------------------------------
  # 1. The Bridge Service
  # ---------------------------------------------------------------------------
  abs-kosync:
    image: 00jlich/abs-kosync-bridge:latest
    container_name: abs_kosync
    restart: unless-stopped
    depends_on:
      - audiobookshelf
      - kosync
    
    # CRITICAL: Machine Learning libraries need shared memory
    shm_size: '2gb'

    environment:
      - TZ=America/New_York
      # --- Server Connections ---
      - ABS_SERVER=http://audiobookshelf:80
      - ABS_KEY=your_abs_api_key_here
      - KOSYNC_SERVER=http://kosync:3000
      - KOSYNC_USER=admin
      - KOSYNC_KEY=your_kosync_password
      
      # --- Sync Logic ---
      - SYNC_PERIOD_MINS=5
      # Loop Prevention: Ignore small changes caused by rounding errors
      - SYNC_DELTA_ABS_SECONDS=60
      - SYNC_DELTA_KOSYNC_PERCENT=1
      
      # --- Matching Logic ---
      - FUZZY_MATCH_THRESHOLD=80
      - KOSYNC_HASH_METHOD=content  # Use 'content' for KOReader native hashing
      
    volumes:
      # Map the EXACT same folder structure used by your KOReader device if possible,
      # or just the root folder containing your EPUBs.
      - ./library:/books
      - ./bridge_data:/data

  # ---------------------------------------------------------------------------
  # 2. Audiobookshelf (Example)
  # ---------------------------------------------------------------------------
  audiobookshelf:
    image: ghcr.io/advplyr/audiobookshelf:latest
    container_name: audiobookshelf
    ports:
      - 13378:80
    volumes:
      - ./audiobooks:/audiobooks
      - ./abs_config:/config
      - ./abs_metadata:/metadata
    environment:
      - TZ=America/New_York
      # ... add other ABS specific env vars here ...

  # ---------------------------------------------------------------------------
  # 3. KoSync Server (Example)
  # ---------------------------------------------------------------------------
  # Note: There are various KoSync server implementations. 
  # This example uses a generic placeholder structure.
  kosync:
    image: dizzy57/kosync:latest # Or whichever implementation you prefer
    container_name: kosync
    ports:
      - 8081:3000
    environment:
      - TZ=America/New_York
      - KOSYNC_SECRET=supersecretkey
      # ... add other KoSync specific env vars here ...
    volumes:
      - ./kosync_db:/db
```
</details>

### Configuration Variables

| Variable | Default | Description 
|--- | --- | --- |
ABS_SERVER | `None` | URL of your Audiobookshelf server (e.g., `http://abs:13378`)
ABS_KEY | `None` | API Key generated in ABS Settings
KOSYNC_SERVER | `None` | URL of your KoSync server (No trailing slash!)
KOSYNC_USER | `None` | Your KoSync username
KOSYNC_KEY | `None` | Your KoSync password
SYNC_PERIOD_MINS | `5` | How often to check for progress updates
SYNC_DELTA_ABS_SECONDS | `60` | Ignore audiobook changes smaller than X seconds (Loop prevention)
SYNC_DELTA_KOSYNC_PERCENT | `1` | Ignore ebook changes smaller than X% (Loop prevention)
FUZZY_MATCH_THRESHOLD | `80` | Confidence score (0-100) required for fuzzy matching
KOSYNC_HASH_METHOD | `content` | content (Recommended/KOReader default) or filename (Legacy)

## 📖 Usage Guide
1. The Matching Wizard
Before syncing, you must link an Audiobook to an Ebook.

``` 
docker-compose run --rm abs-kosync python src/main.py match
```

  1.1. Select the Audiobook from the list.
  1.2. Select the Ebook from the list.
  1.3. Done. The job is queued. You can close the terminal. The container logs will show the transcription progress.

2. Monitoring
Check the logs to see the sync in action:

```
docker-compose logs -f abs-kosync

```

- Processing: Shows download and transcription status.
- Syncing: Shows exactly what text is being matched and the calculated XPath.

## 🌟 Star History

[![Star History Chart](https://api.star-history.com/svg?repos=j-lich/abs-kosync-bridge&type=date&legend=top-left)](https://www.star-history.com/#j-lich/abs-kosync-bridge&type=date&legend=top-left)

## 🛠️ Troubleshooting

- 404 Errors: Ensure KOSYNC_SERVER does not end with a slash /.
- OOM / Crashes: If the container restarts during transcription, try increasing swap space on your host or ensure shm_size: '2gb' is set in docker-compose.
- Sync Loops: If progress keeps bouncing back and forth, increase SYNC_DELTA_ABS_SECONDS.
- Ensure TZ is set the same for all containers (bridge, kosync, abs) - This is to ensure accurate alignment of the most up to date date/time stamp.

## 📄 License

Distributed under the MIT License.

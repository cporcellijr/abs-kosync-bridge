# Configuration

> [!NOTE]
> All configuration is managed via the **Web UI** at `/settings`.
> Environment variables can be used for initial bootstrapping, but values set in the database (via UI) take precedence.

## Web UI Settings

The most convenient way to manage configuration is via the **Settings** page in the Web UI. Changes made here are applied instantly (triggering a soft restart).

### Split-Port Security (Optional)

You can configure the system to listen on two separate ports:

1. **Primary Port (8080)**: Hosts the Admin Dashboard and all API routes. Keep this private/LAN-only.
2. **KOSync Port**: Hosts *only* the KOSync protocol routes needed for KOReader devices. This is safe to expose to the internet.

To enable this mode, set the `KOSYNC_PORT` environment variable (e.g., `KOSYNC_PORT=5758`) and map it in Docker.

```yaml
ports:
  - "8080:5757"   # Admin Dashboard
  - "5758:5758"   # Sync Protocol (Internet Safe)
```

### Integrations

#### KOSync (KOReader)

- **Server**: Your KOSync server URL (e.g., `https://koreader.mydomain.com/api/koreader`).
- **Username**: Your KOSync username.
- **Password**: Your KOSync password.
- **Save Hash Method**: How KOReader calculates document integrity. Keep as default (`content`) unless you know what you're doing.

#### Storyteller

- **Storyteller URL**: URL to your Storyteller instance.
- **Storyteller Username / Password**: Credentials for your Storyteller admin account.
- **Sync Mode**: REST API only. The bridge communicates exclusively via the Storyteller API.
- **Storyteller Assets Path (Optional)**: Root path containing Storyteller `assets/`.
  - Expected structure: `{assets_root}/assets/{book_title}/transcriptions/`
  - If your Docker volume is `/path/to/storyteller/assets:/storyteller/assets`, set this value to `/storyteller`.
  - This setting is optional and can be configured in the Web UI (no compose env var required).
- **Storyteller Backfill**: Settings includes a maintenance action to scan all Storyteller-linked books, ingest available transcript JSON files, and rebuild alignment maps without re-running SMIL/Whisper.
- **Forge Staging Directory (Optional env)**: `PROCESSING_DIR` controls temporary Forge staging before files are atomically presented to Storyteller.
  - Default is `/tmp`, so no dedicated `PROCESSING_DIR` volume mount is required.

> [!NOTE]
> The legacy method of mapping a local Storyteller database (`/storyteller_data`) has been removed. The bridge now communicates strictly via the Storyteller API.

#### Hardcover.app

- **Enable**: Toggle `HARDCOVER_ENABLED` to `true`.
- **API Token**: Your personal API token from [hardcover.app/account/api](https://hardcover.app/account/api).
- **Behavior**: Write-only tracking. The bridge auto-matches books by title/author and updates your reading progress and status (e.g., marks as "Finished" when complete).

#### Telegram Notifications

- **Enable**: Toggle `TELEGRAM_ENABLED` to `true`.
- **Bot Token**: Your Telegram bot token (from [@BotFather](https://t.me/botfather)).
- **Chat ID**: The chat ID to send messages to (your user ID or a group ID).
- **Min Log Level**: The minimum severity level to forward (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). Default: `ERROR`.

#### Shelfmark

- **Shelfmark URL**: URL to your Shelfmark instance. When configured, a Shelfmark icon appears in the navigation bar for quick access.

#### Booklore

- **Booklore URL**: URL to your Booklore/Calibre-Web instance.
- **API Key**: For managing shelves/collections.
- **Target Library**: (Optional) To prevent cross-library contamination, you can specify the Booklore Library ID to use.

#### CWA (Calibre-Web Automated)

- **CWA Server URL**: URL to your Calibre-Web OPDS feed (e.g. `http://my-calibre-web/opds`).
- **CWA Username/Password**: Credentials for Calibre-Web.
- **Enabled**: Set to `true` to enable.
- **Note**: CWA allows the bridge to download ebooks directly from Calibre-Web for Forge/Sync without needing a local `/books` volume.

#### Audiobookshelf

- **ABS Server URL**: Your ABS instance.
- **ABS API Token**: Your secret token.
- **Limit Search to Library**: (Optional) If set, the bridge will only search for audiobooks within this specific ABS Library ID.

### Transcription Settings

Configure the stalign fallback used when Storyteller JSON and SMIL are unavailable.

> [!TIP]
> Transcript waterfall order is: Storyteller JSON → SMIL extraction → stalign fallback.

| Setting | Default | Description |
| :--- | :--- | :--- |
| **STALIGN_PATH** | `/usr/local/bin/stalign` | Path to the stalign binary. |
| **STALIGN_ENGINE** | `whisper.cpp` | Engine: `whisper.cpp`, `openai-cloud`, `deepgram`, `whisper-server`, `google-cloud`, `microsoft-azure`, `amazon-transcribe`. |
| **STALIGN_WHISPER_MODEL** | `tiny.en` | Model used by the `whisper.cpp` engine. |
| **STALIGN_GRANULARITY** | `sentence` | stalign granularity. `sentence` is recommended. |
| **STALIGN_TIMEOUT_MINS** | `60` | Max stalign runtime per book. |

#### Engine-specific settings

- **openai-cloud**: `STALIGN_OPENAI_API_KEY`, `STALIGN_OPENAI_BASE_URL`, `STALIGN_OPENAI_MODEL`
- **deepgram**: `STALIGN_DEEPGRAM_API_KEY`, `STALIGN_DEEPGRAM_MODEL`
- **whisper-server**: `STALIGN_WHISPER_SERVER_URL`, `STALIGN_WHISPER_SERVER_API_KEY`
- **google-cloud**: `STALIGN_GOOGLE_CLOUD_PROJECT`, `STALIGN_GOOGLE_CLOUD_LOCATION`, `STALIGN_GOOGLE_CLOUD_LANGUAGE`
- **microsoft-azure**: `STALIGN_AZURE_SPEECH_KEY`, `STALIGN_AZURE_SPEECH_REGION`, `STALIGN_AZURE_LANGUAGE`
- **amazon-transcribe**: `STALIGN_AWS_ACCESS_KEY_ID`, `STALIGN_AWS_SECRET_ACCESS_KEY`, `STALIGN_AWS_REGION`, `STALIGN_AWS_LANGUAGE`

#### Legacy setting migration

On first boot with an empty settings table, legacy provider values are mapped automatically:

- `TRANSCRIPTION_PROVIDER=local` → `STALIGN_ENGINE=whisper.cpp`
- `TRANSCRIPTION_PROVIDER=deepgram` → `STALIGN_ENGINE=deepgram`
- `TRANSCRIPTION_PROVIDER=whispercpp` → `STALIGN_ENGINE=whisper-server`
- `WHISPER_MODEL` → `STALIGN_WHISPER_MODEL`
- `DEEPGRAM_API_KEY` / `DEEPGRAM_MODEL` → `STALIGN_DEEPGRAM_*`
- `WHISPER_CPP_URL` → `STALIGN_WHISPER_SERVER_URL`

### Sync Tuning

Advanced settings to fine-tune the synchronization logic.

| Setting | Default | Description |
| :--- | :--- | :--- |
| **Sync Period (Minutes)** | `5` | How often the background sync runs. |
| **ABS Delta (Seconds)** | `60` | Minimum progress change (in seconds) required to trigger an update *from* ABS. |
| **KoSync Delta (%)** | `0.5` | Minimum progress change (0.5%) required to trigger an update *from* KOReader. |
| **KoSync Delta (Words)** | `400` | Minimum word-count change required to trigger a KOSync update (used alongside the % delta). |
| **Fuzzy Match Threshold** | `0.80` | (0.0-1.0) Confidence required for text matching (80%). |
| **Job Retries** | `5` | How many times to retry failed transcription jobs. |
| **Job Retry Delay (Mins)** | `15` | Minutes to wait before retrying a failed transcription job. |

### Advanced Toggles

- **Sync ABS Ebook**: If enabled, also syncs progress to the *ebook* item in ABS (if you have both mapped). This allows you to read the ebook in the ABS web reader and have that progress sync to KOReader.
- **Use KOSync Percentage from Server**: If enabled, uses the raw percentage value returned by the KOSync server instead of performing text-based position matching. Useful if text matching is unreliable for a specific book.
- **XPath Fallback**: Strategy for handling position lookups when exact paths fail.
- **Reprocess on Clear**: (`REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT`) If enabled, clearing a mapping in the UI will also delete the alignment cache, forcing a full re-transcription next time.
- **Instant Sync**: (`INSTANT_SYNC_ENABLED`) Controls whether event-driven instant sync is active. When disabled, the ABS Socket.IO listener and KoSync PUT trigger are both turned off — sync falls back to the regular background poll only.

### Per-Client Polling

By default, Storyteller and Booklore are only checked during the global sync cycle. If you want the bridge to watch those clients more (or less) frequently than everything else, set their poll mode to **Custom**.

| Setting | Default | Description |
| :--- | :--- | :--- |
| **Storyteller Poll Mode** | `global` | `global` uses the normal sync cycle. `custom` polls at its own interval. |
| **Storyteller Poll Interval** | `45s` | How often (in seconds) to check Storyteller for position changes when in `custom` mode. |
| **Booklore Poll Mode** | `global` | `global` uses the normal sync cycle. `custom` polls at its own interval. |
| **Booklore Poll Interval** | `300s` | How often (in seconds) to check Booklore for position changes when in `custom` mode. |

> [!NOTE]
> Per-client polling only watches for changes *from* that client and triggers a targeted sync when one is detected. It is much lighter than a full global sync cycle.

---

## GPU Support (Optional)

For significantly faster transcription (when using `local` provider), you can enable NVIDIA GPU acceleration.

### 1. Install NVIDIA Container Toolkit

Follow the official guide to install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) for your host OS.

### 2. Update Docker Compose

Uncomment/Add the `deploy` section to your `docker-compose.yml`:

```yaml
services:
  abs-kosync:
    # ... other config ...
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

### 3. Configure Settings

In the Web UI Settings:

1. Set **Whisper Device** to `cuda`.
2. Set **Whisper Compute Type** to `float16`.
3. Set **Whisper Model** to `small` or `medium` (GPUs can handle larger models easily).

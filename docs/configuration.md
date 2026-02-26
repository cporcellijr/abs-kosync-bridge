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

Configure the engine used for audio-to-text alignment.

| Setting | Default | Description |
| :--- | :--- | :--- |
| **Provider** | `local` | `local` (faster-whisper), `deepgram`, or `whisper_cpp` (via server). |
| **Whisper Model** | `tiny` | Model size (`tiny`, `base`, `small`, `medium`, `large`). |
| **Whisper Device** | `auto` | `auto`, `cpu`, or `cuda`. See [GPU Support](#gpu-support-optional) below. |
| **Compute Type** | `auto` | Precision (`int8`, `float16`, `float32`). Use `float16` for GPU. |

#### Deepgram

- **API Key**: Your Deepgram API Key.
- **Model**: Specific Deepgram model tier (e.g., `nova-2`).

#### WhisperCPP

- **Server URL**: URL to your running `whisper.cpp` server (e.g. `http://my-whisper-server:8080/inference`).
- **Model**: Now controls the `model` parameter sent to the server (e.g. `small`, `medium`).

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

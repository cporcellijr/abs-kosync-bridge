# Getting Started

## Goal

Get your library syncing in about 10 minutes.

---

## Prerequisites

Before you begin, you should have:

- Docker and Docker Compose
- A working Audiobookshelf server
- An ebook folder on the Docker host
- Optional: KOSync, Booklore, Storyteller, or Hardcover if you want those integrations

---

## Step 1: Gather your ABS details

### Audiobookshelf API token

1. Log into Audiobookshelf.
2. Go to **Settings -> Users -> Your user**.
3. Click **Generate API Token**.
4. Copy the token.

### ABS library ID

If you want searches scoped to one ABS library:

1. Open the audiobook library in Audiobookshelf.
2. Look at the URL.
3. Copy the part after `/library/`.

### Optional service credentials

If you plan to use them, also keep these handy:

- KOSync URL, username, and password
- Booklore URL, username, and password
- Storyteller URL, username, and password

---

## Step 2: Prepare a working directory

```bash
mkdir ~/abs-kosync
cd ~/abs-kosync
mkdir data
```

---

## Step 3: Create `docker-compose.yml`

```yaml title="docker-compose.yml"
services:
  abs-kosync:
    container_name: abs_kosync
    image: ghcr.io/cporcellijr/abs-kosync-bridge:latest
    restart: unless-stopped
    ports:
      - "8080:5757"
      # - "5758:5758"  # Optional: expose the sync-only port when using KOSYNC_PORT=5758
    environment:
      - TZ=America/New_York
      - LOG_LEVEL=INFO
      # - KOSYNC_PORT=5758  # Optional: enable split-port mode
      # Configure ABS, KOSync, Booklore, Storyteller, and other services in the Web UI.
    volumes:
      - ./data:/data
      - /path/to/ebooks:/books
      # - /path/to/storyteller/library:/storyteller_library  # Optional: Forge output
      # - /path/to/storyteller/assets:/storyteller/assets    # Optional: Storyteller transcript ingest
```

### Split-port mode

By default, port `8080` exposes the full web UI and API.

If you want to expose only the KOSync endpoint to the internet:

1. Uncomment `KOSYNC_PORT=5758`.
2. Uncomment the `5758:5758` port mapping.
3. Keep `8080` private to your LAN or reverse proxy.

!!! tip "Optional Integrations"
    It is usually easiest to start with the minimal compose file above and finish configuration in the Web UI.

    If you enable Booklore, the bridge can use it for both ebook matching and Booklore audiobook sources.

    If you mount Storyteller assets at `/storyteller/assets`, set **Storyteller Assets Path** in Settings to `/storyteller`.
    The assets path can be configured entirely in the UI; `STORYTELLER_ASSETS_DIR` is optional.

---

## Step 4: Start the service

```bash
docker compose up -d
```

Check the logs:

```bash
docker compose logs -f
```

---

## Step 5: Finish configuration in the Web UI

1. Open `http://localhost:8080/settings`.
2. Enter your **Audiobookshelf Server URL**, **API Token**, and **Library ID**.
3. Add any optional services you want to use:
   - KOSync
   - Booklore
   - Storyteller
   - Hardcover
4. If you mounted Storyteller assets, set **Storyteller Assets Path** to `/storyteller`.
5. Click **Save Settings** and wait for the app to restart.

---

## Step 6: Create your first mapping

You can start in either of these ways:

### Suggestions

1. Open **Suggestions**.
2. Click **Scan Library**.
3. Review the likely pairs.
4. Add the good ones to the queue.
5. Click **Process All**.

### Add Book

1. Open **Add Book**.
2. Pick an ABS audiobook, a Booklore audiobook, or leave audio on **None / Skip** for an ebook-only link.
3. Optionally pick a Storyteller title.
4. Pick the standard ebook.
5. Click **Create Mapping**.

That is enough to get syncing started. The normal background cycle runs every 5 minutes by default, and instant sync can react faster when supported by the source.

# Getting Started

## 🎯 Goal

Get your audiobooks and ebooks syncing in 10 minutes!

---

## Prerequisites

Before you begin, ensure you have the following:

- **Docker** and **Docker Compose** installed.
- An **Audiobookshelf** server running.
- (Optional) A **Booklore** instance (for syncing ebooks).
- (Optional) A **KOReader** sync server (either the official one, a self-hosted instance, or none if just syncing ABS <-> Storyteller).
- Your ebook and audiobook files accessible to the host machine.

---

## Step 1: Get Your API Keys

### Audiobookshelf API Key

1. Log into your ABS server.
2. Go to **Settings** → **Users** → Your user.
3. Click **"Generate API Token"**.
4. Copy the token.

### (Optional) Find Your ABS Library ID

If you want to limit the sync mapping search to a specific library (recommended for performance):

1. In ABS, go to your audiobook library.
2. Look at the URL: `https://your-server.com/library/LIBRARY_ID_HERE`
3. Copy that ID.

### (Optional) KOSync Credentials

If using KOReader sync:

- Your Calibre/KOSync username and password.
- KOSync server URL (usually `https://your-calibre.com/api/koreader`).

### (Optional) Booklore Credentials

If using Booklore:

- Your Booklore server URL.
- Username and password.

---

## Step 2: Prepare Your Work Directory

Create a directory for the application on your server:

```bash
mkdir ~/abs-kosync
cd ~/abs-kosync
mkdir data
```

---

## Step 3: Create docker-compose.yml

Copy this template and fill in YOUR values:

```yaml title="docker-compose.yml"
services:
  abs-kosync:
    image: ghcr.io/cporcellijr/abs-kosync-bridge:latest
    container_name: abs_kosync
    restart: unless-stopped
    ports:
      - "8080:5757" # Admin Panel (Keep Private)
      # - "5758:5758" # Sync Protocol (Safe to Expose)
    environment:
      - TZ=America/New_York
      - LOG_LEVEL=INFO
      
      # NOTE: All configuration (ABS, etc.) is managed in the Web UI.
      
    volumes:
      # === REQUIRED ===
      - ./data:/data                    # App data
      - /path/to/ebooks:/books          # Your EPUB library
      
      # === OPTIONAL: Forge ===
      - /path/to/storyteller/library:/storyteller_library
      # === OPTIONAL: Storyteller transcript ingestion ===
      - /path/to/storyteller/assets:/storyteller/assets
```

### Security Note: Split-Port Mode

By default, the container listens on port **8080** (mapped to 5757 int). This port exposes **everything**: the Admin Dashboard, Settings, and API.

If you want to expose the KOSync endpoint to the internet (for syncing on the go) but keep the Dashboard private, you can use **Split-Port Mode**:

1. Set `KOSYNC_PORT=5758` (or any other port) in your environment variables.
2. Map that port in `docker-compose.yml` (as shown in the commented example).

!!! tip "Optional Integrations"
    You can configure KOSync, Storyteller, and other integrations via enviroment variables during bootstrap, but it is easier to do it later in the Web UI!
    
    If you mount Storyteller assets at `/storyteller/assets`, set **Storyteller Assets Path** in Settings to `/storyteller`.
    The assets path can be configured fully in the UI; `STORYTELLER_ASSETS_DIR` env is optional.

---

## Step 4: Start the Service

```bash
docker compose up -d
```

Check the logs to ensure everything is running smoothly:

```bash
docker compose logs -f
```

---

## Step 5: Initial Configuration

1. Open your browser and go to `http://localhost:8080/settings` (or your server IP).
2. Enter your **Audiobookshelf Server URL** and **API Key** (from Step 1).
3. (Optional) Enter your **KOSync**, **Booklore**, or **Storyteller** credentials.
4. (Optional) If Storyteller assets are mounted, set **Storyteller Assets Path** to `/storyteller`.
5. Click **Save Settings**. The application will restart automatically to apply changes.

---

## Step 6: Create Your First Mapping

1. Go to the **Match** page (or click "Single Match" on the dashboard).
2. **Search** for an audiobook (e.g., "The Martian").
3. **Select** the audiobook from the first column.
4. (Optional) Select a **Storyteller** artifact if one was found. If not, choose "None".
5. **Select** the standard EPUB file from the third column.
6. Click **Create Mapping**.

That's it! The system will now automatically sync progress between your audiobook and ebook every 5 minutes (default).

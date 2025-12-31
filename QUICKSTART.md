# Quick Start Guide - ABS-KoSync Enhanced

## 🎯 Goal
Get your audiobooks and ebooks syncing in 10 minutes!

---

## Step 1: Get Your API Keys

### Audiobookshelf API Key
1. Log into your ABS server
2. Go to **Settings** → **Users** → Your user
3. Click **"Generate API Token"**
4. Copy the token (starts with `eyJ...`)

### Find Your ABS Library ID
1. In ABS, go to your audiobook library
2. Look at the URL: `https://your-server.com/library/LIBRARY_ID_HERE`
3. Copy that ID

### KOSync Credentials
- Your Calibre/KOSync username and password
- KOSync server URL (usually `https://your-calibre.com/api/koreader`)

---

## Step 2: Prepare Your Folders

Create a directory for the app:
```bash
mkdir ~/abs-kosync
cd ~/abs-kosync
```

---

## Step 3: Create docker-compose.yml

Copy this template and fill in YOUR values:

```yaml
services:
  abs-kosync-enhanced:
    image: your-username/abs-kosync-enhanced:latest
    container_name: abs-kosync
    restart: unless-stopped
    
    environment:
      # REQUIRED - Fill these in!
      - ABS_SERVER=https://YOUR_ABS_SERVER.com
      - ABS_KEY=YOUR_API_TOKEN_HERE
      - ABS_LIBRARY_ID=YOUR_LIBRARY_ID
      
      - KOSYNC_SERVER=https://YOUR_CALIBRE_SERVER.com/api/koreader
      - KOSYNC_USER=YOUR_USERNAME
      - KOSYNC_KEY=YOUR_PASSWORD
      - KOSYNC_HASH_METHOD=content
      
      # OPTIONAL - Basic settings
      - TZ=America/New_York
      - LOG_LEVEL=INFO
      - SYNC_PERIOD_MINS=5
      - FUZZY_MATCH_THRESHOLD=88
    
    volumes:
      # REQUIRED
      - ./data:/data
      - /path/to/your/ebooks:/books
    
    ports:
      - "8080:5757"
```

**Replace these:**
- `YOUR_ABS_SERVER.com` → Your Audiobookshelf URL
- `YOUR_API_TOKEN_HERE` → The API key from Step 1
- `YOUR_LIBRARY_ID` → The library ID from Step 1
- `YOUR_CALIBRE_SERVER.com` → Your Calibre/KOSync server
- `YOUR_USERNAME` → Your KOSync username
- `YOUR_PASSWORD` → Your KOSync password
- `/path/to/your/ebooks` → Where your EPUB files are

---

## Step 4: Start the Container

```bash
docker compose up -d
```

Check if it's running:
```bash
docker compose logs -f
```

Look for:
- ✅ Connected to Audiobookshelf
- ✅ Connected to KOSync Server

Press `Ctrl+C` to exit logs.

---

## Step 5: Open the Web UI

Open your browser to: **http://localhost:8080**

You should see the ABS-KoSync dashboard!

---

## Step 6: Create Your First Mapping

1. Click **"Single Match"** button
2. Find an audiobook you're currently listening to
3. Find the matching ebook
4. Click **"Create Mapping"**

That's it! The sync will start automatically.

---

## 🎉 Success!

Your progress should now sync between:
- Audiobookshelf (when listening)
- KOReader (when reading)

The system checks every 5 minutes by default.

---

## 🔧 Troubleshooting

### Container won't start?
```bash
docker compose logs
```
Look for error messages about API keys or server connections.

### Can't access web UI?
- Check if port 8080 is available: `docker compose ps`
- Try http://localhost:8080 or http://YOUR_SERVER_IP:8080

### Sync not working?
- Wait 5 minutes (default sync period)
- Check the dashboard - does it show progress?
- Make sure you're using the same ebook file in both systems

---

## ➡️ What's Next?

Once basic sync is working, you can add:
- **Storyteller integration** (three-way sync)
- **Book Linker** (automated Storyteller workflows)
- **Booklore integration** (shelf organization)

See the full README.md for advanced features!

---

## 🆘 Need Help?

- Check the logs: `docker compose logs -f`
- Read the full README.md
- Open an issue on GitHub with your logs


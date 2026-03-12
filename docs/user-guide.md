# User Guide

This guide covers the main workflows in the ABS-KoSync Enhanced web UI.

## Dashboard

The **Dashboard** is the main status view for your library.

It shows:

- **Active Syncs** for every tracked mapping
- **Unified Progress** across all connected clients
- **Source badges** so you can tell whether the audio side is coming from Audiobookshelf or Booklore
- **Direct links** into supported services, including Booklore audio when a mapping uses it
- Quick access to **Add Book**, **Batch Match**, **Suggestions**, **Forge**, **Settings**, and **Logs**

If a book is significantly out of sync, the card is highlighted so you can spot it quickly.

---

## Sync Modes

Each mapping runs in one of two modes.

### 1. Audiobook Sync

This is the normal mode when a mapping has an audiobook source.

- The audio source can be **Audiobookshelf** or **Booklore**.
- The text side can include a standard ebook, a Storyteller artifact, or both.
- The bridge prefers Storyteller transcript timing when available, then falls back to SMIL, then Whisper.

Use this when you want listening and reading progress to stay aligned.

### 2. Ebook-Only Sync

This mode tracks reading progress without attaching an audiobook source.

- Create it by leaving audio on **None / Skip** in **Add Book**.
- You can still link a standard ebook, a Storyteller title, or both.
- Ebook-only links skip audiobook preparation work, so they activate faster.

Use this when you only want reading sync between KOReader, Booklore, Storyteller, and optional ABS ebook progress.

---

## Real-Time Sync

The bridge still runs a normal background sync every 5 minutes by default, but it can also react much faster when supported.

### Instant triggers

1. **Audiobookshelf playback**: when playback changes in Audiobookshelf, the bridge can sync shortly after the activity settles.
2. **KOReader push**: if you use the built-in KOSync bridge, KOReader can push progress directly to the bridge.

### Per-client polling

Storyteller and Booklore can also use their own polling intervals:

- **Global** uses the normal background cycle.
- **Custom** lets that client be checked on its own schedule.

This is useful when you often read directly in Storyteller or Booklore and want the bridge to notice sooner.

---

## Add Book

**Add Book** is the main manual linking tool.

### Step 1: Choose audio

You can choose:

- An **Audiobookshelf audiobook**
- A **Booklore audiobook**
- **None / Skip** for an ebook-only link

The source badge on each card tells you where the audiobook came from.

### Step 2: Choose Storyteller (optional)

If Storyteller is configured, you can also link a Storyteller title.

- Pick the Storyteller card when you want read-along support.
- Leave it on **None / Skip** if you only want the standard ebook.

### Step 3: Choose the standard ebook

The bridge can pull ebook choices from:

1. Audiobookshelf ebook files
2. Booklore
3. CWA
4. Local `/books` files

### Final actions

- **Create Mapping** creates the link immediately.
- **Forge & Match** stages the book for Storyteller processing first, then finishes the link when Forge completes.

If you skip audio, **Create Mapping** makes an ebook-only link instead.

---

## Batch Match

**Batch Match** is the queue-based version of Add Book.

Use it when you want to review multiple links and process them together.

- Queue entries can use **Audiobookshelf** or **Booklore** as the audio source.
- You can attach a standard ebook, a Storyteller title, or both.
- Queue items created from **Suggestions** land here too.

---

## Suggestions

The **Suggestions** page is a review workspace for likely matches that are not linked yet.

### What it does

- Scans unmatched titles in your library
- Shows likely audiobook + ebook pairs
- Lets you review one suggestion at a time
- Sends approved picks into the same queue used by Batch Match

### Scan options

- **Scan Library** reuses cached results so repeat scans are faster.
- **Full Refresh** ignores the previous cache and rescans the whole unmatched library.

### Actions

- **Add to Queue** sends the current pick to the batch queue.
- **Dismiss** hides a suggestion for now.
- **Never** hides it permanently so it does not come back.

Suggestions can create:

- Standard ABS-backed links
- Booklore-audio links
- Ebook-only links
- Storyteller-only links when that is enough for the workflow you want

---

## Forge

**Forge** prepares books for Storyteller read-along processing.

### What Forge stages

- Audio from **Audiobookshelf** or **Booklore**
- Text from **Booklore**, **CWA**, **local files**, or **Audiobookshelf**

### Two ways to use it

1. **Forge & Match from Add Book**
   - Starts the Storyteller workflow
   - Finishes the mapping when processing completes

2. **Standalone Forge page**
   - Stages a Storyteller-ready book without creating a sync mapping yet

Forge uses `PROCESSING_DIR` if you set it, and falls back to `/tmp` if you do not.

---

## Storyteller Transcript Tools

When Storyteller transcript assets are available, the bridge can use them directly for better timing and locator quality.

### Storyteller Backfill

Use **Settings -> Storyteller Backfill** to:

- Re-scan all Storyteller-linked books
- Ingest any newly available transcript assets
- Rebuild alignment data without rerunning Whisper

This is useful after importing old Storyteller assets or fixing your Storyteller assets mount.

---

## Booklore Audio

Booklore is no longer only an ebook target.

You can now use **Booklore audiobooks** in:

- **Add Book**
- **Batch Match**
- **Suggestions**
- **Forge**
- The main **Dashboard**

If Booklore imports change and results look stale, open **Settings** and run **Refresh Booklore Cache**.

---

## Auto-Discovery

If KOReader devices sync directly to the built-in KOSync bridge, the system can discover new reading activity automatically.

### What happens

1. KOReader pushes progress to the bridge.
2. The bridge looks for a matching audiobook source.
3. One of two things happens:
   - If a likely audio match exists, the bridge creates a **Suggestion** for you to review.
   - If no audiobook source is found, it can create an **ebook-only** workflow instead.

Suggestions still require approval before a real mapping is created.

---

## Management

### Delete mapping

Stops syncing that book. It does not delete your original media files.

### Reset progress

Clears the stored sync state for a mapping.

If **Regenerate Missing Data on Reset** is enabled, the bridge can also rebuild missing alignment data when needed.

### Logs

Open **Logs** to inspect live application logs for matching, syncing, Storyteller ingest, Booklore refreshes, and background jobs.

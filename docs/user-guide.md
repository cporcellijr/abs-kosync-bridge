# User Guide

This guide covers the features and workflows available in the ABS-KoSync Enhanced Web UI.

## Dashboard

The **Dashboard** is your command center. It shows:

- **Active Syncs**: A list of all books currently being tracked.
- **Sync Status**: Real-time progress bars for each book.
- **Recent Activity**: A log of the latest sync actions.
- **Single Match**: Manually link an audiobook to an ebook.
- **Batch Match**: Switch to the Batch Matcher view.

---

## Sync Modes

When creating a mapping, the system operates in one of two modes:

### 1. Audiobook Sync (Default)

Links an **Audiobook** in ABS to an **Ebook** in KOReader (and other platforms).

- **Mechanism**: Transcription-based alignment.
- **Use Case**: Listening on phone, reading on Kindle/Kobo.
- **Process**: Mapping creates a transcript of the audiobook, then uses that text to find the corresponding page in the ebook.

### 2. Ebook-Only Sync

Links your ebook status across platforms.

- **Mechanism**: Direct percentage/text position sync.
- **Triggers**:
  - **Internal KOSync**: Instant (Push-based).
  - **ABS/Booklore**: Periodic Polling (every ~5 mins).
- **Supported Clients**:
  - **KOReader** (via Internal KOSync Server)
  - **Booklore** (Requires `BOOKLORE_ENABLED=true`)
  - **ABS Ebook** (Requires `SYNC_ABS_EBOOK=true`)
- **Hardcover**: Updates Hardcover.app presence (if configured).
- **Note**: If you only use Booklore and ABS Web Reader (no KOReader), syncs will occur only during the periodic poll interval.

---

## Matcher

The **Matcher** allows you to link an Audiobookshelf audiobook to a specific EPUB file in your `/books` directory.

### Single Match

1. **Search**: Type the name of the audiobook. The system queries your ABS library.
2. **Select Audio**: Click on the correct audiobook result.
3. **Select Text**: (Optional if using Storyteller) Select your **standard/retail EPUB** here.
    - *Note: You can skip this step if you only want to link the audio to a Storyteller artifact.*
4. **Storyteller Tri-Link**: If you have a Storyteller artifact, paste its UUID or select it here.
    - **Benefit**: If you linked a retail EPUB in step 3, you can read that lightweight file on your e-reader while maintaining sync. If you skipped step 3, the system will use the Storyteller file for syncing.
5. **Create Mapping**: Click the button to confirm the link.

### Batch Match

For users with large libraries, the **Batch Matcher** speeds up the process.

1. **Queue**: Add multiple books to the matching queue.
2. **Auto-Suggest**: The system will attempt to find the best matching EPUB for each audiobook based on filename similarity.
3. **Confirm**: Review the suggestions and confirm them in bulk.

---

## Forge (Storyteller Integration)

> [!NOTE]
> Forge replaces the legacy "Book Linker" tool.

The **Forge** is a powerful tool designed to prepare "Synced Books" for **Storyteller**. It handles the complex file management required to get audio and text aligned.

### What it does

1. **Staging**: Copies audio files (from ABS) and ebook files (from Booklore/Local/CWA) to a temporary staging area in the correct `StorytellerLibrary/Title/` structure.
2. **Processing**: Automatically triggers Storyteller to scan and process the new book.
3. **Cleanup**: Monitors the output folder. Once Storyteller finishes generating the "Readaloud" ebook, Forge automatically deletes the source files to save space.

### When to use it

Use Forge if you want the **immersive Read-Along experience** in the Storyteller app.

> [!IMPORTANT]
> **Active Processing**: Unlike the old Book Linker, Forge is an **active** tool. It communicates directly with the Storyteller API to trigger processing. Ensure your Storyteller integration is configured in Settings.

---

## Auto-Discovery (Internal KOSync Only)

If you configure your KOReader devices to sync directly to **this bridge** (using the Internal KOSync Server), the system can automatically detect and link books for you. This is the **only way** to automatically create Ebook-Only links without manual approval.

### How it works

1. **Push**: You open a book in KOReader. It pushes progress to the bridge instantaneously.
2. **Match**: The bridge looks for a matching audiobook in your ABS library.
3. **Action**:
    - **Audiobook Found**: Creates a **Suggestion** on your Dashboard (requires approval).
    - **No Audiobook Found**: Automatically creates an **Ebook-Only Sync** mapping, allowing immediate cross-device tracking.

> [!IMPORTANT]
> **Trigger Constraint**: This "Auto-Creation" feature requires using the **Internal KOSync Server**. Polling traditional services (ABS/Booklore) will simply create **Suggestions**, which always require manual approval.

---

## Suggestions

The system can also intelligently suggest mappings based on activity, but these always require manual confirmation.

**Sources**:

1. **Audiobookshelf (Periodic Polling)**: When the bridge sees you listening to an unmapped audiobook, it suggests a matching ebook.
2. **Internal KOSync (Push)**: When you read an unmapped ebook that *does* have a matching audiobook.

**Actions**:

- **Review**: Opens the Matcher pre-filled.
- **Dismiss**: Removes the suggestion.

---

## Management

### Editing Mappings

You can edit or delete existing mappings from the Dashboard.

- **Delete**: Stops syncing the book. Does NOT delete the files.
- **Reset Progress**: Clears the stored sync state if things get out of whack.

### Viewing Logs

Navigate to **Logs** in the sidebar to view the live application logs. This is useful for troubleshooting why a specific book might not be syncing.

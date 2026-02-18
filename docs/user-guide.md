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

The **Matcher** is where you link an Audiobookshelf audiobook to its corresponding text format. The new **3-Column Interface** makes it easy to select the best source for your needs.

### 1. The 3-Step Flow

#### Step 1: Select Audiobook (Required)

First, search for your audiobook. The system queries your ABS library. Select the correct title to proceed.

#### Step 2: Storyteller Link (Optional)

If you use **Storyteller**, the system will try to find a matching "Artifact" (the processed ebook) in your Storyteller library.

- **Match Found**: Select the card to link it via its precise UUID.
- **No Match/Incorrect**: Select **"None - Do not link"**. This tells the system to ignore Storyteller for this book and use the standard ebook instead.

> [!TIP]
> **Tri-Link Feature**: You can link *both* a Storyteller Artifact (for voice-synced reading) AND a standard Retail EPUB (for lightweight reading on other devices). The progress will sync between all three!

#### Step 3: Select Standard Ebook (Fallback)

Select the standard EPUB file you want to use for regular sync. The system searches multiple sources:

1. **ABS Direct**: Checks if the Audiobook item in ABS already contains an EPUB file.
2. **Booklore**: Checks your curated metadata database.
3. **CWA**: Searches your Calibre-Web Automated OPDS feed.
4. **ABS Search**: Searches other ABS libraries for a matching title.
5. **Filesystem**: Scans your local `/books` directory.

**Source Badges**: Look for the colored badges on ebook cards to know where the file is coming from (e.g., `BOOKLORE`, `ABS`, `CWA`, `LOCAL`).

### Creating the Mapping

Once you've made your selections, click **Create Mapping**. The system will download any necessary files (from CWA/ABS) and begin the alignment process.

### Batch Match

For users with large libraries, the **Batch Matcher** speeds up the process.

1. **Queue**: Add multiple books to the matching queue.
2. **Auto-Suggest**: The system will attempt to find the best matching EPUB for each audiobook based on filename similarity.
3. **Confirm**: Review the suggestions and confirm them in bulk.

---

## Forge (Storyteller Integration)

The **Forge** is a powerful tool designed to prepare "Synced Books" for **Storyteller**. It handles the complex file management required to get audio and text aligned.

### What it does

1. **Staging**: Copies audio files (from ABS) and ebook files (from Booklore/Local/CWA) to a temporary staging area in the correct `StorytellerLibrary/Title/` structure.
2. **Processing**: Automatically triggers Storyteller to scan and process the new book.
3. **Cleanup**: Monitors the output folder. Once Storyteller finishes generating the "Readaloud" ebook, Forge automatically deletes the source files to save space.

### Two Ways to Forge

#### 1. Auto-Forge (from the Matcher — Recommended)

When creating a mapping in the **Matcher**, you can choose **"Forge & Match"** instead of "Create Mapping". This:

- Stages and processes the book through Storyteller automatically.
- **Automatically creates the sync mapping** once Storyteller finishes, linking the ABS audiobook, the original EPUB, and the new Storyteller artifact in one step.

This is the recommended workflow for most users.

#### 2. Manual Forge (from the Forge page)

The standalone **Forge** page (`/forge`) lets you stage and process a book through Storyteller without creating a sync mapping. Use this if you want to prepare a book for Storyteller independently and then link it manually via the Matcher later.

### When to use it

Use Forge if you want the **immersive Read-Along experience** in the Storyteller app.

> [!IMPORTANT]
> **Active Processing**: Forge is an **active** tool. It communicates directly with the Storyteller API to trigger processing. Ensure your Storyteller integration is configured in Settings.

---

## Auto-Discovery (Internal KOSync Only)

If you configure your KOReader devices to sync directly to **this bridge** (using the Built-in KOSync Bridge), the system can automatically detect and link books for you. This is the **only way** to automatically create Ebook-Only links without manual approval.

> [!TIP]
> **Built-in KOSync Bridge**: In the Settings page, under **KOSync Integration**, enable the integration and check **"Use Built-in KOSync Bridge"**. The UI will display the URL to enter into KOReader's sync settings.

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

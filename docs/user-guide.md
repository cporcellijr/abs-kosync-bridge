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
3. **Select Text**: The system will list available EPUB files. Use the search bar to filter.
4. **Create Mapping**: Click the button to confirms the link.

### Batch Match
For users with large libraries, the **Batch Matcher** speeds up the process.

1. **Queue**: Add multiple books to the matching queue.
2. **Auto-Suggest**: The system will attempt to find the best matching EPUB for each audiobook based on filename similarity.
3. **Confirm**: Review the suggestions and confirm them in bulk.

---

## Book Linker (Storyteller Ingest)

> [!WARNING]
> The Book Linker is **NOT** a sync tool. It is a file management utility.

The **Book Linker** is designed specifically to help you **create "Synced Books" for Storyteller**. It simplifies the process of getting the audio files and ebook files into the correct folder structure for Storyteller to ingest them.

### What it does:
1.  **Finds Files**: Locates the audio files in ABS and the ebook file in your local directory.
2.  **Copies Files**: Copies *both* to a temporary `/processing` folder.
3.  **Triggers Ingest**: Moves the folder to the Storyteller ingest directory, effectively telling Storyteller "Here is a new book pair, please generate the text-audio alignment."

### When to use it:
Use this feature **ONLY** if you want to use the Storyteller app's "Read-Along" feature (listening while the text highlights). If you just want to keep your place in sync between ABS and KOReader, you do **not** need the Book Linker.

### Important Constraints

> [!WARNING]
> **No Process Started**: The Book Linker is a **passive** file manager. It does **NOT** start or control any Storyteller process. It simply moves files to where Storyteller expects them.

> [!CAUTION]
> **Volume Mapping (Storyteller V2)**:
> Storyteller V2 is stateful and tracks file paths. If you map `/processing` and `/storyteller/library` to **different** folders, you cause a conflict:
> 1. Bridge copies to `/processing`. Storyteller imports it (Path: `/processing/Book`).
> 2. Bridge moves it to `/storyteller/library`. Storyteller sees a "new" book.
> 3. Storyteller tries to open the old `/processing` path, which is now empty -> **Error 500**.
>
> **Best Practice**: Ensure your processing workflows do not confuse Storyteller's database, or accept that manual cleanup in Storyteller may be required. Ideally, point Storyteller to watch the *final* destination, not the temporary processing folder.

---

## Auto-Discovery (Internal KOSync Only)

If you configure your KOReader devices to sync directly to **this bridge** (using the Internal KOSync Server), the system can automatically detect and link books for you. This is the **only way** to automatically create Ebook-Only links without manual approval.

### How it works
1.  **Push**: You open a book in KOReader. It pushes progress to the bridge instantaneously.
2.  **Match**: The bridge looks for a matching audiobook in your ABS library.
3.  **Action**:
    *   **Audiobook Found**: Creates a **Suggestion** on your Dashboard (requires approval).
    *   **No Audiobook Found**: Automatically creates an **Ebook-Only Sync** mapping, allowing immediate cross-device tracking.

> [!IMPORTANT]
> **Trigger Constraint**: This "Auto-Creation" feature requires using the **Internal KOSync Server**. Polling traditional services (ABS/Booklore) will simply create **Suggestions**, which always require manual approval.

---

## Suggestions

The system can also intelligently suggest mappings based on activity, but these always require manual confirmation.

**Sources**:
1.  **Audiobookshelf (Periodic Polling)**: When the bridge sees you listening to an unmapped audiobook, it suggests a matching ebook.
2.  **Internal KOSync (Push)**: When you read an unmapped ebook that *does* have a matching audiobook.

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

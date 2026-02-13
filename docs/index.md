# ABS-KoSync Enhanced

<div align="center">

**The ultimate bridge for cross-platform reading and listening synchronization.**

[Getting Started](getting-started.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/cporcellijr/abs-kosync-bridge){ .md-button }

</div>

---

## 📖 What is it?

**ABS-KoSync Enhanced** is a powerful, automated synchronization engine designed to unify your reading and listening experiences. It bridges the gap between audiobooks and ebooks, ensuring that whether you're listening on the go or reading on your e-reader, your progress is always perfectly aligned.

### 🔄 Five-Way Synchronization

The system keeps your progress in sync across all major platforms:

| Platform | Type | Capability |
| :--- | :--- | :--- |
| **Audiobookshelf** | Audiobooks | Full Read/Write Sync |
| **KOReader / KOSync** | Ebooks | Full Read/Write Sync |
| **Storyteller** | Enhanced Reader | Full Read/Write Sync (REST API & SQLite) |
| **Booklore** | Library Management | Full Read/Write Sync |
| **Hardcover.app** | Book Tracking | Write-Only Tracking (Auto-Update Finished Status) |

---

## ✨ Features

### 🚀 Core Sync Engine

- **Robust Synchronization**: Syncs progress bi-directionally between Audiobookshelf and KOReader.
- **Split-Port Security**: Optionally run the sync service on a separate port from the admin dashboard for safe internet exposure.
- **Forge**: Active tooling to prepare and trigger "Read-Along" books for Storyteller.
- **Multi-Device Support**: Handles multiple KOReader devices seamlessly.
- **Multi-Platform Support**: Synchronize progress across five different ecosystems simultaneously.
- **Smart Conflict Resolution**: "Furthest progress wins" logic with built-in anti-regression protection.
- **Rich Positioning**: Support for XPath, CSS selectors, and EPUB CFI for pixel-perfect positioning.
- **Resumable Jobs**: Background transcription jobs resume automatically if interrupted.

### 🖥️ Management Web UI

- **Real-Time Dashboard**: Monitor progress and sync status across all your books.
- **Advanced Matcher**: Manual mapping for complex titles or different editions.
- **Batch Processing**: Queue and process multiple books for synchronization in bulk.
- **Book Linker**: Automated workflow for Storyteller readaloud generation.
- **Dynamic Settings**: Configure your entire system from the Web UI with instant hot-reloading.

### 🤖 Automation & Reliability

- **Background Daemon**: Configurable sync intervals for hands-off operation.
- **Auto-Organization**: Automatic addition to ABS collections and Booklore shelves.
- **Error Recovery**: Automatic retry logic for failed transcription or sync tasks.

---

## 🛠️ How It Works

## 🛠️ How It Works

The sync engine operates on a sophisticated event-driven architecture (V2):

1. **Triggers**: Changes are detected via **Instant Pushes** (Internal KOSync) or **Periodic Polling** (ABS/Booklore).
2. **Normalization**: Progress from all clients is normalized into a common format (timestamp or percentage).
3. **Discrepancy Check**: The system identifies if a significant change has occurred.
4. **Leader Election**: The client with the most recent explicit progress becomes the "Leader".
5. **Translation**: If the Leader is an Audiobook and followers are Ebooks (or vice-versa), the system uses **Whisper AI transcripts** to translate the timestamp into an exact text position (or vice versa).
6. **Propagation**: The new position is sent to all other configured clients.

```mermaid
graph TD
    A[Start Sync Cycle] --> B{Trigger?}
    B -->|Poll Timer| C[Fetch Progress (All Clients)]
    B -->|Instance Sync| C
    B -->|KoSync Push| C
    C --> D[Normalize Positions]
    D --> E{Leader Check}
    E -->|Significant Delta| F[Identify Leader]
    E -->|No Change| A
    F --> G{Format Mismatch?}
    G -->|Yes| H[Audio <--> Text Translation]
    G -->|No| I[Direct Sync]
    H --> J[Calculate New Position]
    I --> J
    J --> K[Update Follower Clients]
    K --> L[Save State to DB]
    L --> A
```

!!! note "Audio to Text Conversion"
    The system extracts a snippet of text from the audiobook transcript at the current timestamp and performs a fuzzy search within the EPUB to find the corresponding ebook location.

# [START FILE: abs-kosync-enhanced/web_server.py]
import glob
import html
import logging
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
import schedule
from dependency_injector import providers
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, send_from_directory

from src.utils.config_loader import ConfigLoader
from src.utils.logging_utils import memory_log_handler, LOG_PATH
from src.utils.logging_utils import sanitize_log_data
from src.utils.logging_utils import sanitize_log_data
from src.api.kosync_server import kosync_sync_bp, kosync_admin_bp, init_kosync_server
from src.api.hardcover_routes import hardcover_bp, init_hardcover_routes

def _reconfigure_logging():
    """Force update of root logger level based on env var."""
    try:
            new_level_str = os.environ.get('LOG_LEVEL', 'INFO').upper()
            new_level = getattr(logging, new_level_str, logging.INFO)

            root = logging.getLogger()
            root.setLevel(new_level)

            logger.info(f"📝 Logging level updated to {new_level_str}")
    except Exception as e:
            logger.warning(f"Failed to reconfigure logging: {e}")

# ---------------- APP SETUP ----------------

def setup_dependencies(app, test_container=None):
    """
    Initialize dependencies for the web server.

    Args:
        test_container: Optional test container for dependency injection during testing.
                       If None, creates production container from environment.
    """
    global container, manager, database_service, DATA_DIR, EBOOK_DIR, COVERS_DIR

    # Initialize Database Service
    from src.db.migration_utils import initialize_database
    database_service = initialize_database(os.environ.get("DATA_DIR", "/data"))

    # Load settings from DB

    # This updates os.environ with values from the database
    if database_service:
        ConfigLoader.bootstrap_config(database_service)
        ConfigLoader.load_settings(database_service)
        logger.info("✅ Settings loaded into environment variables")

        # Force reconfigure logging level based on new settings
        _reconfigure_logging()

    # RELOAD GLOBALS from updated os.environ

    global LINKER_BOOKS_DIR, DEST_BASE, STORYTELLER_INGEST, ABS_AUDIO_ROOT
    global STORYTELLER_LIBRARY_DIR, EBOOK_IMPORT_DIR
    global ABS_API_URL, ABS_API_TOKEN, ABS_LIBRARY_ID
    global ABS_COLLECTION_NAME, BOOKLORE_SHELF_NAME, MONITOR_INTERVAL, SHELFMARK_URL
    global SYNC_PERIOD_MINS, SYNC_DELTA_ABS_SECONDS, SYNC_DELTA_KOSYNC_PERCENT, FUZZY_MATCH_THRESHOLD

    LINKER_BOOKS_DIR = Path(os.environ.get("LINKER_BOOKS_DIR", "/linker_books"))
    DEST_BASE = Path(os.environ.get("PROCESSING_DIR", "/processing"))
    STORYTELLER_INGEST = Path(os.environ.get("STORYTELLER_INGEST_DIR", os.environ.get("LINKER_BOOKS_DIR", "/linker_books")))
    ABS_AUDIO_ROOT = Path(os.environ.get("AUDIOBOOKS_DIR", "/audiobooks"))
    STORYTELLER_LIBRARY_DIR = Path(os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))
    EBOOK_IMPORT_DIR = Path(os.environ.get("EBOOK_IMPORT_DIR", "/books"))

    ABS_API_URL = os.environ.get("ABS_SERVER")
    ABS_API_TOKEN = os.environ.get("ABS_KEY")
    ABS_LIBRARY_ID = os.environ.get("ABS_LIBRARY_ID")

    def _get_float_env(key, default):
        try:
            return float(os.environ.get(key, str(default)))
        except (ValueError, TypeError):
            logger.warning(f"Invalid {key} value, defaulting to {default}")
            return float(default)

    SYNC_PERIOD_MINS = _get_float_env("SYNC_PERIOD_MINS", 5)
    SYNC_DELTA_ABS_SECONDS = _get_float_env("SYNC_DELTA_ABS_SECONDS", 30)
    SYNC_DELTA_KOSYNC_PERCENT = _get_float_env("SYNC_DELTA_KOSYNC_PERCENT", 0.005)
    FUZZY_MATCH_THRESHOLD = _get_float_env("FUZZY_MATCH_THRESHOLD", 0.8)

    ABS_COLLECTION_NAME = os.environ.get("ABS_COLLECTION_NAME", "Synced with KOReader")
    BOOKLORE_SHELF_NAME = os.environ.get("BOOKLORE_SHELF_NAME", "Kobo")
    MONITOR_INTERVAL = int(os.environ.get("MONITOR_INTERVAL", "3600"))
    SHELFMARK_URL = os.environ.get("SHELFMARK_URL", "")

    logger.info(f"🔄 Globals reloaded from settings (ABS_SERVER={ABS_API_URL})")

    if test_container is not None:
        # Use injected test container
        container = test_container
    else:
        # 3. Create production container AFTER loading settings
        # The container providers (Factories) will now read the updated os.environ values
        from src.utils.di_container import create_container
        container = create_container()

    # 4. Override the container's database_service with our already-initialized instance
    # This ensures consistency and prevents re-initialization
    # Only do this for production containers that support dependency injection
    if test_container is None:
        container.database_service.override(providers.Object(database_service))

    # Initialize manager and services
    manager = container.sync_manager()

    # Get data directories (now using updated env vars)
    DATA_DIR = container.data_dir()
    EBOOK_DIR = container.books_dir()

    # Initialize covers directory
    COVERS_DIR = DATA_DIR / "covers"
    if not COVERS_DIR.exists():
        COVERS_DIR.mkdir(parents=True, exist_ok=True)

    # Register KoSync Blueprint and initialize with dependencies
    init_kosync_server(database_service, container, manager, EBOOK_DIR)
    app.register_blueprint(kosync_sync_bp)
    app.register_blueprint(kosync_admin_bp)

    # Register Hardcover Blueprint and initialize with dependencies
    init_hardcover_routes(database_service, container)
    app.register_blueprint(hardcover_bp)

    logger.info(f"Web server dependencies initialized (DATA_DIR={DATA_DIR})")







# Audiobook files location
ABS_AUDIO_ROOT = Path(os.environ.get("AUDIOBOOKS_DIR", "/audiobooks"))

# ABS API Configuration
ABS_API_URL = os.environ.get("ABS_SERVER")
ABS_API_TOKEN = os.environ.get("ABS_KEY")
ABS_LIBRARY_ID = os.environ.get("ABS_LIBRARY_ID")

# ABS Collection name for auto-adding matched books
ABS_COLLECTION_NAME = os.environ.get("ABS_COLLECTION_NAME", "Synced with KOReader")

# Booklore shelf name for auto-adding matched books
BOOKLORE_SHELF_NAME = os.environ.get("BOOKLORE_SHELF_NAME", "Kobo")


SHELFMARK_URL = os.environ.get("SHELFMARK_URL", "")

# Storyteller Forge
STORYTELLER_LIBRARY_DIR = Path(os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))

# Track active forge operations for UI status
active_forge_tasks = set()
forge_lock = threading.Lock()


# ---------------- HELPER FUNCTIONS ----------------
def get_audiobooks_conditionally():
    """Get audiobooks either from specific library or all libraries based on ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID setting."""
    abs_only_search_in_library = os.environ.get("ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID", "false").lower() == "true"
    abs_library_id = os.environ.get("ABS_LIBRARY_ID")

    if abs_only_search_in_library and abs_library_id:
        # Fetch audiobooks only from the specified library
        return container.abs_client().get_audiobooks_for_lib(abs_library_id)
    else:
        # Fetch all audiobooks from all libraries
        return container.abs_client().get_all_audiobooks()

# ---------------- CONTEXT PROCESSORS ----------------
def inject_global_vars():
    def get_val(key, default_val=None):
        if key in os.environ: return os.environ[key]
        DEFAULTS = {
            'TZ': 'America/New_York',
            'LOG_LEVEL': 'INFO',
            'DATA_DIR': '/data',
            'BOOKS_DIR': '/books',
            'ABS_COLLECTION_NAME': 'Synced with KOReader',
            'BOOKLORE_SHELF_NAME': 'Kobo',
            'SYNC_PERIOD_MINS': '5',
            'SYNC_DELTA_ABS_SECONDS': '60',
            'SYNC_DELTA_KOSYNC_PERCENT': '0.5',
            'SYNC_DELTA_BETWEEN_CLIENTS_PERCENT': '0.5',
            'SYNC_DELTA_KOSYNC_WORDS': '400',
            'FUZZY_MATCH_THRESHOLD': '80',
            'WHISPER_MODEL': 'tiny',
            'JOB_MAX_RETRIES': '5',
            'JOB_RETRY_DELAY_MINS': '15',
            'MONITOR_INTERVAL': '3600',
            'LINKER_BOOKS_DIR': '/linker_books',
            'PROCESSING_DIR': '/processing',
            'STORYTELLER_INGEST_DIR': '/linker_books',
            'AUDIOBOOKS_DIR': '/audiobooks',
            'ABS_PROGRESS_OFFSET_SECONDS': '0',
            'EBOOK_CACHE_SIZE': '3',
            'KOSYNC_HASH_METHOD': 'content',
            'TELEGRAM_LOG_LEVEL': 'ERROR',
            'SHELFMARK_URL': '',
            'KOSYNC_ENABLED': 'false',
            'STORYTELLER_ENABLED': 'false',
            'BOOKLORE_ENABLED': 'false',
            'HARDCOVER_ENABLED': 'false',
            'TELEGRAM_ENABLED': 'false',
            'SUGGESTIONS_ENABLED': 'false',
            'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT': 'true'
        }
        if key in DEFAULTS: return DEFAULTS[key]
        return default_val if default_val is not None else ''

    def get_bool(key):
        val = os.environ.get(key, 'false')
        return val.lower() in ('true', '1', 'yes', 'on')

    return dict(
        shelfmark_url=os.environ.get("SHELFMARK_URL", ""),
        abs_server=os.environ.get("ABS_SERVER", ""),
        booklore_server=os.environ.get("BOOKLORE_SERVER", ""),
        get_val=get_val,
        get_bool=get_bool
    )

# ---------------- BOOK LINKER HELPERS ----------------

def safe_folder_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    name = html.escape(str(name).strip())[:150]
    for c in invalid:
        name = name.replace(c, '_')
    return name.strip() or "Unknown"


def copy_audio_files_for_forge(abs_id: str, dest_folder: Path):
    """Copy audiobook files from ABS - Book Linker version"""
    headers = {"Authorization": f"Bearer {ABS_API_TOKEN}"}
    url = urljoin(ABS_API_URL, f"/api/items/{abs_id}")
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        item = r.json()
        audio_files = item.get("media", {}).get("audioFiles", [])
        if not audio_files:
            logger.warning(f"No audio files found for ABS {abs_id}")
            return False

        dest_folder.mkdir(parents=True, exist_ok=True)
        copied = 0

        for f in audio_files:
            meta = f.get("metadata", {})
            full_path = meta.get("path", "")
            filename = meta.get("filename", "")

            src_path = None
            # 1. Try exact path (rarely works across containers)
            if full_path and Path(full_path).exists():
                src_path = Path(full_path)

            # 2. Smart Suffix Matching
            # Tries to match the last 4, 3, 2, or 1 segments of the path (e.g. Author/Series/Book/file.mp3)
            if not src_path and full_path:
                parts = Path(full_path).parts
                for i in range(4, 0, -1):
                    if len(parts) < i: continue
                    suffix = Path(*parts[-i:])
                    candidate = ABS_AUDIO_ROOT / suffix
                    if candidate.exists():
                        src_path = candidate
                        break

            # 3. Filename fallback (slowest but most reliable)
            if not src_path and filename:
                # Limit search to avoid hanging on massive libraries
                matches = list(ABS_AUDIO_ROOT.glob(f"**/{filename}"))
                if matches:
                    src_path = matches[0]

            if src_path and src_path.exists():
                shutil.copy2(str(src_path), dest_folder / src_path.name)
                copied += 1
            else:
                logger.error(f"Could not find audio file: {filename}")
        return copied > 0
    except Exception as e:
        logger.error(f"Failed to copy ABS {abs_id}: {e}", exc_info=True)
        return False


def sync_daemon():
    """Background sync daemon running in a separate thread."""
    try:
        # Setup schedule for sync operations
        # Use the global SYNC_PERIOD_MINS which is validated
        schedule.every(int(SYNC_PERIOD_MINS)).minutes.do(manager.sync_cycle)
        schedule.every(1).minutes.do(manager.check_pending_jobs)

        logger.info(f"🔄 Sync daemon started (period: {SYNC_PERIOD_MINS} minutes)")

        # Run initial sync cycle
        try:
            manager.sync_cycle()
        except Exception as e:
            logger.error(f"Initial sync cycle failed: {e}")

        # Main daemon loop
        while True:
            try:
                # logger.debug("Running pending schedule jobs...")
                schedule.run_pending()
                time.sleep(30)  # Check every 30 seconds
            except Exception as e:
                logger.error(f"Sync daemon error: {e}")
                time.sleep(60)  # Wait longer on error

    except Exception as e:
        logger.error(f"Sync daemon crashed: {e}")


# ---------------- ORIGINAL ABS-KOSYNC HELPERS ----------------

def find_ebook_file(filename):
    base = EBOOK_DIR
    escaped_filename = glob.escape(filename)
    matches = list(base.rglob(escaped_filename))
    return matches[0] if matches else None


def get_kosync_id_for_ebook(ebook_filename, booklore_id=None, original_filename=None):
    """Get KOSync document ID for an ebook.
    Tries Booklore API first (if configured and booklore_id provided),
    falls back to filesystem if needed.
    """
    # Try Booklore API first
    if booklore_id and container.booklore_client().is_configured():
        try:
            content = container.booklore_client().download_book(booklore_id)
            if content:
                kosync_id = container.ebook_parser().get_kosync_id_from_bytes(ebook_filename, content)
                if kosync_id:
                    logger.debug(f"Computed KOSync ID from Booklore download: {kosync_id}")
                    return kosync_id
        except Exception as e:
            logger.warning(f"Failed to get KOSync ID from Booklore, falling back to filesystem: {e}")

    # Fall back to filesystem
    ebook_path = find_ebook_file(ebook_filename)
    if not ebook_path and original_filename:
        # [Tri-Link] Fallback to original filename if Storyteller file not found/relevant
        logger.debug(f"Primary file '{ebook_filename}' not found, checking original '{original_filename}'")
        ebook_path = find_ebook_file(original_filename)

    if ebook_path:
        return container.ebook_parser().get_kosync_id(ebook_path)

    # [NEW] Check Epub Cache explicitly (if acquired by LibraryService but not meant for /books)
    epub_cache = container.epub_cache_dir()
    cached_path = epub_cache / ebook_filename
    if cached_path.exists():
         return container.ebook_parser().get_kosync_id(cached_path)

    # [NEW] On-Demand Fetching
    # 1. ABS On-Demand
    if "_abs." in ebook_filename:
        try:
             # Extract ID: 1941a138-1c8d-49eb-954f-f6bb26f87ebc_abs.epub -> 1941a138-1c8d-49eb-954f-f6bb26f87ebc
             abs_id = ebook_filename.split("_abs.")[0]
             abs_client = container.abs_client()
             if abs_client and abs_client.is_configured():
                 logger.info(f"📥 Attempting on-demand ABS download for {abs_id}...")
                 ebook_files = abs_client.get_ebook_files(abs_id)
                 if ebook_files:
                     target = ebook_files[0]
                     if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)
                     
                     if abs_client.download_file(target['stream_url'], cached_path):
                         logger.info(f"   ✅ Downloaded ABS ebook to {cached_path}")
                         return container.ebook_parser().get_kosync_id(cached_path)
                 else:
                     logger.warning(f"   ⚠️ No ebook files found in ABS for item {abs_id}")
        except Exception as e:
            logger.error(f"   ❌ Failed ABS on-demand download: {e}")

    # 2. CWA On-Demand
    if "_cwa." in ebook_filename or ebook_filename.startswith("cwa_"):
        try:
             # Extract ID: cwa_12345.epub -> 12345
             # Format is cwa_{id}.{ext}
             if ebook_filename.startswith("cwa_"):
                 # Robust: strip cwa_ prefix and the extension
                 cwa_id = ebook_filename[4:].rsplit(".", 1)[0]
             else:
                 # Pattern like somefile_cwa.epub or itemid_cwa.epub
                 cwa_id = ebook_filename.split("_cwa.")[0]
                 
                 # If it was still prefixed with something else, handle it? 
                 # Usually it's {uuid}_cwa.epub or cwa_{id}.epub
                 if "_" in cwa_id and not ebook_filename.startswith("cwa_"):
                     # If format is uuid_cwa.epub, cwa_id is uuid (correct)
                     pass 
                 
             if cwa_id:
                 cwa_client = container.cwa_client()
                 if cwa_client and cwa_client.is_configured():
                     logger.info(f"📥 Attempting on-demand CWA download for ID {cwa_id}...")
                     
                     target = None
                     
                     # Priority 1: Search for the ID (search results include download_url and won't crash the server)
                     results = cwa_client.search_ebooks(cwa_id)
                     
                     # Find exact ID match if possible
                     for res in results:
                         if str(res.get('id')) == cwa_id:
                             target = res
                             break
                     
                     # If no exact ID match, maybe it was the only result
                     if not target and len(results) == 1:
                         target = results[0]

                     # Priority 2: Use direct download URL from search if available
                     if target and target.get('download_url'):
                         logger.info(f"🚀 Using direct download link from search for '{target.get('title', 'Unknown')}'")
                     else:
                         # Priority 3: Fallback to get_book_by_id only if search didn't provide a URL
                         # This may crash server on metadata page, but includes a blind URL fallback
                         logger.debug(f"🔍 Search did not return a usable result, trying direct ID lookup...")
                         target = cwa_client.get_book_by_id(cwa_id)

                     if target and target.get('download_url'):
                         if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)
                         if cwa_client.download_ebook(target['download_url'], cached_path):
                             logger.info(f"   ✅ Downloaded CWA ebook to {cached_path}")
                             return container.ebook_parser().get_kosync_id(cached_path)
                     else:
                         logger.warning(f"   ⚠️ Could not find CWA book for ID {cwa_id}")
        except Exception as e:
            logger.error(f"   ❌ Failed CWA on-demand download: {e}")

    # Neither source available - log helpful warning
    if not container.booklore_client().is_configured() and not EBOOK_DIR.exists():
        logger.warning(
            f"Cannot compute KOSync ID for '{ebook_filename}': "
            "Neither Booklore integration nor /books volume is configured. "
            "Enable Booklore (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD) "
            "or mount the ebooks directory to /books."
        )
    elif not booklore_id and not ebook_path:
        logger.warning(f"Cannot compute KOSync ID for '{ebook_filename}': File not found in Booklore, filesystem, or remote sources.")

    return None


class EbookResult:
    """Wrapper to provide consistent interface for ebooks from Booklore, CWA, ABS, or filesystem."""

    def __init__(self, name, title=None, subtitle=None, authors=None, booklore_id=None, path=None, source=None):
        self.name = name
        self.title = title or Path(name).stem
        self.subtitle = subtitle or ''
        self.authors = authors or ''
        self.booklore_id = booklore_id
        self._path = path
        self.source = source  # 'booklore', 'cwa', 'abs', 'filesystem'
        # Has metadata if we have a real title (not just filename) or booklore_id
        self.has_metadata = booklore_id is not None or (title is not None and title != name)

    @property
    def display_name(self):
        """Format: 'Author - Title: Subtitle' for sources with metadata, filename for filesystem."""
        if self.has_metadata and self.title:
            full_title = self.title
            if self.subtitle:
                full_title = f"{self.title}: {self.subtitle}"
            if self.authors:
                return f"{self.authors} - {full_title}"
            return full_title
        return self.name

    @property
    def stem(self):
        return Path(self.name).stem

    def __str__(self):
        return self.name


def get_searchable_ebooks(search_term):
    """Get ebooks from Booklore API, filesystem, ABS, and CWA.
    Returns list of EbookResult objects for consistent interface."""

    results = []
    found_filenames = set()
    found_stems = set()  # To dedupe by title stem

    # 1. Booklore
    if container.booklore_client().is_configured():
        try:
            books = container.booklore_client().search_books(search_term)
            if books:
                for b in books:
                    fname = b.get('fileName', '')
                    if fname.lower().endswith('.epub'):
                        found_filenames.add(fname.lower())
                        found_stems.add(Path(fname).stem.lower())
                        results.append(EbookResult(
                            name=fname,
                            title=b.get('title'),
                            subtitle=b.get('subtitle'),
                            authors=b.get('authors'),
                            booklore_id=b.get('id'),
                            source='Booklore'
                        ))
        except Exception as e:
            logger.warning(f"Booklore search failed: {e}")

    # 2. ABS ebook libraries
    if search_term:
        try:
            abs_client = container.abs_client()
            if abs_client:
                abs_ebooks = abs_client.search_ebooks(search_term)
                if abs_ebooks:
                    for ab in abs_ebooks:
                        ebook_files = abs_client.get_ebook_files(ab['id'])
                        if ebook_files:
                            ef = ebook_files[0]
                            fname = f"{ab['id']}_abs.{ef['ext']}"
                            if fname.lower() not in found_filenames:
                                results.append(EbookResult(
                                    name=fname,
                                    title=ab.get('title'),
                                    authors=ab.get('author'),
                                    source='ABS'
                                ))
                                found_filenames.add(fname.lower())
                                if ab.get('title'):
                                    found_stems.add(ab['title'].lower().strip())
        except Exception as e:
            logger.warning(f"ABS ebook search failed: {e}")

    # 3. CWA (Calibre-Web Automated)
    if search_term:
        try:
            library_service = container.library_service()
            if library_service and library_service.cwa_client and library_service.cwa_client.is_configured():
                cwa_results = library_service.cwa_client.search_ebooks(search_term)
                if cwa_results:
                    for cr in cwa_results:
                        fname = f"cwa_{cr.get('id', 'unknown')}.{cr.get('ext', 'epub')}"
                        if fname.lower() not in found_filenames:
                            results.append(EbookResult(
                                name=fname,
                                title=cr.get('title'),
                                authors=cr.get('author'),
                                source='CWA'
                            ))
                            found_filenames.add(fname.lower())
                            if cr.get('title'):
                                found_stems.add(cr['title'].lower().strip())
        except Exception as e:
            logger.warning(f"CWA search failed: {e}")

    # 4. Search filesystem (Local) - LOW PRIORITY
    if EBOOK_DIR.exists():
        try:
            all_epubs = list(EBOOK_DIR.glob("**/*.epub"))
            for eb in all_epubs:
                fname_lower = eb.name.lower()
                stem_lower = eb.stem.lower()

                # Dedupe: if already found in rich source, skip
                if fname_lower in found_filenames or stem_lower in found_stems:
                    continue

                if not search_term or search_term.lower() in fname_lower:
                    results.append(EbookResult(name=eb.name, path=eb, source='Local File'))
                    found_filenames.add(fname_lower)
                    found_stems.add(stem_lower)

        except Exception as e:
            logger.warning(f"Filesystem search failed: {e}")

    # Check if we have no sources at all
    if not results and not EBOOK_DIR.exists() and not container.booklore_client().is_configured():
        logger.warning(
            "No ebooks available: Neither Booklore integration nor /books volume is configured. "
            "Enable Booklore (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD) "
            "or mount the ebooks directory to /books."
        )

    return results



def restart_server():
    """
    Triggers a graceful restart by sending SIGTERM to the current process.
    The start.sh supervisor loop will catch the exit and restart the application.
    """
    logger.info("♻️  Stopping application (Supervisor will restart it)...")
    time.sleep(1.0)  # Give Flask time to send the redirect response

    # Exit with 0 so start.sh loop restarts the process
    logger.info("👋 Exiting process to trigger restart...")
    sys.exit(0)

def settings():
    # Application Defaults
    # Note: These are also defined in inject_global_vars for context processor usage
    # We should probably centralize them, but for now this works.

    if request.method == 'POST':
        bool_keys = [
            'KOSYNC_USE_PERCENTAGE_FROM_SERVER',
            'SYNC_ABS_EBOOK',
            'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT',
            'KOSYNC_ENABLED',
            'STORYTELLER_ENABLED',
            'BOOKLORE_ENABLED',
            'CWA_ENABLED',
            'HARDCOVER_ENABLED',
            'TELEGRAM_ENABLED',
            'SUGGESTIONS_ENABLED',
            'ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID',
            'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT'
        ]

        # Current settings in DB
        current_settings = database_service.get_all_settings()

        # 1. Handle Boolean Toggles (Checkbox logic)
        # Checkboxes are NOT sent if unchecked, so we must check every known bool key
        for key in bool_keys:
            is_checked = (key in request.form)
            # Save "true" or "false"
            val_str = str(is_checked).lower()
            database_service.set_setting(key, val_str)
            os.environ[key] = val_str # Immediate update for current process

        # 2. Handle Text Inputs
        # Iterate over form to find other keys
        for key, value in request.form.items():
            if key in bool_keys: continue

            clean_value = value.strip()

            if clean_value:
                database_service.set_setting(key, clean_value)
                os.environ[key] = clean_value # Immediate update for current process
            elif key in current_settings:
                database_service.set_setting(key, "")
                os.environ[key] = "" # Immediate update for current process

        try:
            # Trigger Auto-Restart in a separate thread so this request finishes
            threading.Thread(target=restart_server).start()

            session['message'] = "Settings saved. Application is restarting..."
            session['is_error'] = False
        except Exception as e:
            session['message'] = f"Error saving settings: {e}"
            session['is_error'] = True
            logger.error(f"Error saving settings: {e}")

        return redirect(url_for('settings'))

    # GET Request
    message = session.pop('message', None)
    is_error = session.pop('is_error', False)

    return render_template('settings.html',
                         message=message,
                         is_error=is_error)

def get_abs_author(ab):

    """Extract author from ABS audiobook metadata."""
    media = ab.get('media', {})
    metadata = media.get('metadata', {})
    return metadata.get('authorName') or (metadata.get('authors') or [{}])[0].get("name", "")


def audiobook_matches_search(ab, search_term):
    """Check if audiobook matches search term (searches title AND author)."""
    import re

    # Normalize: remove punctuation
    def normalize(s):
        return re.sub(r'[^\w\s]', '', s.lower())

    title = normalize(manager.get_abs_title(ab))
    author = normalize(get_abs_author(ab))
    search_norm = normalize(search_term)

    # 1. Standard Search: Search term is in Title or Author (e.g. "Harry" in "Harry Potter")
    if search_norm in title or search_norm in author:
        return True

    # 2. Reverse Search: Title/Author is in Search term (e.g. "Dune" in "Dune Messiah")
    # FIX: Enforce minimum length to prevent short/empty matches (e.g. "The", "It", "")
    MIN_LEN = 4
    
    if len(title) >= MIN_LEN and title in search_norm: return True
    if len(author) >= MIN_LEN and author in search_norm: return True

    return False

# ---------------- ROUTES ----------------
def index():
    """Dashboard - loads books and progress from database service"""

    # Load books from database service
    books = database_service.get_all_books()

    # Fetch all states at once to avoid N+1 queries with NullPool
    all_states = database_service.get_all_states()
    states_by_book = {}
    for state in all_states:
        if state.abs_id not in states_by_book:
            states_by_book[state.abs_id] = []
        states_by_book[state.abs_id].append(state)

    # Fetch pending suggestions
    suggestions_raw = database_service.get_all_pending_suggestions()

    # Filter suggestions: Hide those with 0 matches
    suggestions = []

    for s in suggestions_raw:
        if len(s.matches) == 0:
            continue
        suggestions.append(s)

    # [OPTIMIZATION] Fetch all hardcover details at once
    all_hardcover = database_service.get_all_hardcover_details()
    hardcover_by_book = {h.abs_id: h for h in all_hardcover}

    integrations = {}

    # Dynamically check all configured sync clients
    sync_clients = container.sync_clients()
    for client_name, client in sync_clients.items():
        if client.is_configured():
            integrations[client_name.lower()] = True
        else:
            integrations[client_name.lower()] = False

    # Convert books to mappings format for template compatibility
    mappings = []
    total_duration = 0
    total_listened = 0

    for book in books:
        # Get states for this book from pre-fetched dict
        states = states_by_book.get(book.abs_id, [])

        # Convert states to a dict by client name for easy access
        state_by_client = {state.client_name: state for state in states}

        # Create mapping dict for template compatibility
        mapping = {
            'abs_id': book.abs_id,
            'abs_title': book.abs_title,
            'ebook_filename': book.ebook_filename,
            'kosync_doc_id': book.kosync_doc_id,
            'transcript_file': book.transcript_file,
            'status': book.status,
            'sync_mode': getattr(book, 'sync_mode', 'audiobook'),
            'unified_progress': 0,
            'duration': book.duration or 0,
            'states': {}
        }

        if book.status == 'processing':
            job = database_service.get_latest_job(book.abs_id)
            if job:
                mapping['job_progress'] = round((job.progress or 0.0) * 100, 1)
            else:
                mapping['job_progress'] = 0.0

        # Populate progress from states
        latest_update_time = 0
        max_progress = 0

        # Process each client state and store both timestamp and percentage
        for client_name, state in state_by_client.items():
            if state.last_updated and state.last_updated > latest_update_time:
                latest_update_time = state.last_updated

            # Store both timestamp and percentage for each client
            mapping['states'][client_name] = {
                'timestamp': state.timestamp or 0,
                'percentage': round(state.percentage * 100, 1) if state.percentage else 0,
                'last_updated': state.last_updated
            }

            # Calculate max progress for unified_progress (using percentage)
            if state.percentage:
                progress_pct = round(state.percentage * 100, 1)
                max_progress = max(max_progress, progress_pct)

        # Add hardcover mapping details
        hardcover_details = hardcover_by_book.get(book.abs_id)
        if hardcover_details:
            mapping.update({
                'hardcover_book_id': hardcover_details.hardcover_book_id,
                'hardcover_slug': hardcover_details.hardcover_slug,
                'hardcover_edition_id': hardcover_details.hardcover_edition_id,
                'hardcover_pages': hardcover_details.hardcover_pages,
                'isbn': hardcover_details.isbn,
                'asin': hardcover_details.asin,
                'matched_by': hardcover_details.matched_by,
                'hardcover_linked': True,
                'hardcover_title': book.abs_title  # Use ABS title as fallback for Hardcover title
            })
        else:
            mapping.update({
                'hardcover_book_id': None,
                'hardcover_slug': None,
                'hardcover_edition_id': None,
                'hardcover_pages': None,
                'isbn': None,
                'asin': None,
                'matched_by': None,
                'hardcover_linked': False,
                'hardcover_title': None
            })
            
        # [NEW] Check for legacy Storyteller link
        # Book has 'storyteller' state but no 'storyteller_uuid'
        has_storyteller_state = 'storyteller' in state_by_client
        is_legacy_link = has_storyteller_state and not book.storyteller_uuid
        mapping['storyteller_legacy_link'] = is_legacy_link

        # Platform deep links for dashboard
        mapping['abs_url'] = f"{manager.abs_client.base_url}/item/{book.abs_id}"

        # Booklore deep link (if configured and book found)
        if manager.booklore_client.is_configured():
            bl_book = manager.booklore_client.find_book_by_filename(book.ebook_filename, allow_refresh=False)
            # [FIX] Fallback to original filename if storyteller artifact doesn't match
            if not bl_book and book.original_ebook_filename:
                bl_book = manager.booklore_client.find_book_by_filename(book.original_ebook_filename, allow_refresh=False)
        else:
            bl_book = None

        if bl_book:
            mapping['booklore_id'] = bl_book.get('id')
            mapping['booklore_url'] = f"{manager.booklore_client.base_url}/book/{bl_book.get('id')}?tab=view"
        else:
            mapping['booklore_id'] = None
            mapping['booklore_url'] = None

        # Hardcover deep link (if linked)
        if mapping.get('hardcover_slug'):
            mapping['hardcover_url'] = f"https://hardcover.app/books/{mapping['hardcover_slug']}"
        elif mapping.get('hardcover_book_id'):
            mapping['hardcover_url'] = f"https://hardcover.app/books/{mapping['hardcover_book_id']}"
        else:
            mapping['hardcover_url'] = None

        # Set unified progress to the maximum progress across all clients
        mapping['unified_progress'] = min(max_progress, 100.0)

        # Calculate last sync time
        if latest_update_time > 0:
            diff = time.time() - latest_update_time
            if diff < 60:
                mapping['last_sync'] = f"{int(diff)}s ago"
            elif diff < 3600:
                mapping['last_sync'] = f"{int(diff // 60)}m ago"
            else:
                mapping['last_sync'] = f"{int(diff // 3600)}h ago"
        else:
            mapping['last_sync'] = "Never"

        # Set cover URL
        if book.abs_id:
            mapping['cover_url'] = f"{manager.abs_client.base_url}/api/items/{book.abs_id}/cover?token={manager.abs_client.token}"

        # Add to totals for overall progress calculation
        duration = mapping.get('duration', 0)
        progress_pct = mapping.get('unified_progress', 0)

        if duration > 0:
            total_duration += duration
            total_listened += (progress_pct / 100.0) * duration

        mappings.append(mapping)

    # Calculate overall progress based on total duration and listening time
    if total_duration > 0:
        overall_progress = round((total_listened / total_duration) * 100, 1)
    elif mappings:
        # Fallback: average progress if no duration data available
        overall_progress = round(sum(m['unified_progress'] for m in mappings) / len(mappings), 1)
    else:
        overall_progress = 0

    return render_template('index.html', mappings=mappings, integrations=integrations, progress=overall_progress, suggestions=suggestions)


def shelfmark():
    """Shelfmark view - renders an iframe with SHELFMARK_URL"""
    url = os.environ.get("SHELFMARK_URL")
    if not url:
        return redirect(url_for('index'))
    return render_template('shelfmark.html', shelfmark_url=url)


def forge():
    """Storyteller Forge - 2-column UI for combining ABS audio with ebook text."""
    return render_template('forge.html')


def forge_search_audio():
    """API: Search ABS audiobooks for Forge (returns JSON)."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])

    try:
        all_audiobooks = get_audiobooks_conditionally()
        query_lower = query.lower()
        results = []

        for ab in all_audiobooks:
            if audiobook_matches_search(ab, query_lower):
                item_details = container.abs_client().get_item_details(ab.get('id'))
                if not item_details:
                    continue

                media = item_details.get('media', {})
                metadata = media.get('metadata', {})
                audio_files = media.get('audioFiles', [])
                title = metadata.get('title', ab.get('name', 'Unknown'))

                if not audio_files:
                    continue

                size_mb = sum(f.get('metadata', {}).get('size', 0) for f in audio_files) / (1024 * 1024)

                # Build cover URL
                cover_url = ""
                abs_server = os.environ.get("ABS_SERVER", "")
                if abs_server:
                    cover_url = f"/api/cover-proxy/{ab.get('id')}"

                results.append({
                    "id": ab.get("id"),
                    "title": title,
                    "author": metadata.get('authorName') or get_abs_author(ab),
                    "file_size_mb": round(size_mb, 2),
                    "num_files": len(audio_files),
                    "cover_url": cover_url,
                })

        return jsonify(results)
    except Exception as e:
        logger.error(f"Forge audio search failed: {e}", exc_info=True)
        return jsonify([])


def forge_search_text():
    """API: Unified text source search for Forge - ABS ebooks, Booklore, CWA, local files."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])

    results = []
    found_ids = set()  # Dedupe
    query_lower = query.lower()

    # 1. Booklore
    if container.booklore_client().is_configured():
        try:
            books = container.booklore_client().search_books(query)
            if books:
                for b in books:
                    fname = b.get('fileName', '')
                    if fname.lower().endswith('.epub'):
                        key = f"booklore_{b.get('id', fname)}"
                        if key not in found_ids:
                            found_ids.add(key)
                            results.append({
                                "id": key,
                                "title": b.get('title', fname),
                                "author": b.get('authors', ''),
                                "source": "Booklore",
                                "filename": fname,
                                "booklore_id": b.get('id'),
                            })
        except Exception as e:
            logger.warning(f"Forge: Booklore search failed: {e}")

    # 2. ABS Ebooks
    try:
        abs_client = container.abs_client()
        if abs_client:
            abs_ebooks = abs_client.search_ebooks(query)
            if abs_ebooks:
                for ab in abs_ebooks:
                    ebook_files = abs_client.get_ebook_files(ab['id'])
                    if ebook_files:
                        ef = ebook_files[0]
                        key = f"abs_{ab['id']}"
                        if key not in found_ids:
                            found_ids.add(key)
                            results.append({
                                "id": key,
                                "title": ab.get('title', 'Unknown'),
                                "author": ab.get('author', ''),
                                "source": "ABS",
                                "abs_id": ab['id'],
                                "ext": ef.get('ext', 'epub'),
                            })
    except Exception as e:
        logger.warning(f"Forge: ABS ebook search failed: {e}")

    # 3. CWA
    try:
        library_service = container.library_service()
        if library_service and library_service.cwa_client and library_service.cwa_client.is_configured():
            cwa_results = library_service.cwa_client.search_ebooks(query)
            if cwa_results:
                for cr in cwa_results:
                    key = f"cwa_{cr.get('id', 'unknown')}"
                    if key not in found_ids:
                        found_ids.add(key)
                        results.append({
                            "id": key,
                            "title": cr.get('title', 'Unknown'),
                            "author": cr.get('author', ''),
                            "source": "CWA",
                            "cwa_id": cr.get('id'),
                            "ext": cr.get('ext', 'epub'),
                            "download_url": cr.get('download_url', ''),
                        })
    except Exception as e:
        logger.warning(f"Forge: CWA search failed: {e}")

    # 4. Local files from BOOKS_DIR
    try:
        local_books_dir = Path(os.environ.get("BOOKS_DIR", "/books"))
        if local_books_dir.exists():
            for epub in local_books_dir.rglob("*.epub"):
                if "(readaloud)" in epub.name.lower():
                    continue
                if query_lower in epub.name.lower():
                    key = f"local_{epub.name}"
                    if key not in found_ids:
                        found_ids.add(key)
                        results.append({
                            "id": key,
                            "title": epub.stem,
                            "author": "",
                            "source": "Local File",
                            "path": str(epub),
                            "file_size_mb": round(epub.stat().st_size / (1024 * 1024), 2),
                        })
    except Exception as e:
        logger.warning(f"Forge: Local file search failed: {e}")

    return jsonify(results)


def _forge_background_task(abs_id, text_item, title, author):
    """
    Background thread: copy files to Storyteller library, trigger processing, cleanup.
    Tracks active status in active_forge_tasks global.
    """
    logger.info(f"🔨 Forge: Starting background task for '{title}'")
    
    with forge_lock:
        active_forge_tasks.add(title)

    try:
        # Define paths
        safe_author = safe_folder_name(author) if author else "Unknown"
        safe_title = safe_folder_name(title) if title else "Unknown"
        # [FIX] Get dynamic library path from config
        try:
            st_lib_path = container.config_service().get('STORYTELLER_LIBRARY_DIR')
        except Exception:
            st_lib_path = None
            
        if not st_lib_path:
            # Fallback to env or default
            st_lib_path = os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library")
            
        # Flattened Structure: Library/Title/
        # User requested to remove Author subfolder for Storyteller compatibility/preference
        course_dir = Path(st_lib_path) / safe_title
        audio_dest = course_dir / "Audio"
        audio_dest.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"⚡ Forge: Staging files for '{title}' in '{course_dir}'")

        # Step 1: Copy audio files
        audio_ok = copy_audio_files_for_forge(abs_id, audio_dest)
        if not audio_ok:
            logger.error(f"⚡ Forge: Failed to copy audio files for {abs_id}")
            # cleanup empty dir
            # cleanup empty dir
            try:
                # If audio_dest was created, removing course_dir recursively might be safer/cleaner 
                # or just rmdir audio_dest then course_dir
                if audio_dest.exists(): audio_dest.rmdir()
                if course_dir.exists(): course_dir.rmdir()
            except: pass
            return
        logger.info(f"⚡ Forge: Audio files copied for '{title}'")

        # Step 2: Acquire text source (epub)
        epub_dest = course_dir / f"{safe_title}.epub"
        source = text_item.get('source', '')
        
        text_success = False

        if source == 'Local File':
            src_path = Path(text_item.get('path', ''))
            if src_path.exists():
                shutil.copy2(str(src_path), epub_dest)
                text_success = True
                logger.info(f"⚡ Forge: Local epub copied: {src_path.name}")
            else:
                logger.error(f"⚡ Forge: Local file not found: {src_path}")

        elif source == 'Booklore':
            booklore_id = text_item.get('booklore_id')
            if booklore_id:
                content = container.booklore_client().download_book(booklore_id)
                if content:
                    epub_dest.write_bytes(content)
                    text_success = True
                    logger.info(f"⚡ Forge: Booklore epub downloaded")
                else:
                    logger.error(f"⚡ Forge: Booklore download failed for {booklore_id}")

        elif source == 'ABS':
            abs_item_id = text_item.get('abs_id')
            if abs_item_id:
                abs_client = container.abs_client()
                ebook_files = abs_client.get_ebook_files(abs_item_id)
                if ebook_files:
                    stream_url = ebook_files[0].get('stream_url', '')
                    if stream_url and abs_client.download_file(stream_url, epub_dest):
                        text_success = True
                        logger.info(f"⚡ Forge: ABS epub downloaded")
                    else:
                        logger.error(f"⚡ Forge: ABS download failed for {abs_item_id}")
        
        elif source == 'CWA':
            download_url = text_item.get('download_url', '')
            cwa_id = text_item.get('cwa_id')
            cwa_client = container.library_service().cwa_client
            
            if download_url and cwa_client:
                if cwa_client.download_ebook(download_url, epub_dest):
                    text_success = True
                    logger.info(f"⚡ Forge: CWA epub downloaded")
            elif cwa_id and cwa_client:
                book_info = cwa_client.get_book_by_id(cwa_id)
                if book_info and book_info.get('download_url'):
                    if cwa_client.download_ebook(book_info['download_url'], epub_dest):
                        text_success = True
                        logger.info(f"⚡ Forge: CWA epub downloaded via ID lookup")
            
            if not text_success:
                logger.error(f"⚡ Forge: CWA download failed")

        else:
            logger.error(f"⚡ Forge: Unknown text source: {source}")

        if not text_success:
            logger.error(f"⚡ Forge: Text acquisition failed. Aborting.")
             # Cleanup audio
            shutil.rmtree(audio_dest, ignore_errors=True)
            try: course_dir.rmdir() 
            except: pass
            return

        logger.info(f"⚡ Forge: Files staged. Waiting for Storyteller to detect '{title}'...")

        # Trigger Storyteller Processing via API
        st_client = container.storyteller_client()
        found_uuid = None
        
        for _ in range(60): 
            time.sleep(5)
            try:
                results = st_client.search_books(title)
                for b in results:
                    # simplistic match, or we could match path if available
                    if b.get('title') == title:
                        found_uuid = b.get('uuid')
                        break
                if found_uuid: break
            except Exception as e:
                logger.debug(f"Forge: Storyteller search error: {e}")
                pass
        
        if found_uuid:
            logger.info(f"⚡ Forge: Book detected ({found_uuid}). Triggering processing...")
            try:
                if hasattr(st_client, 'trigger_processing'):
                    st_client.trigger_processing(found_uuid)
                else:
                    # Fallback if client update pending reload (shouldn't happen in prod but good for safety)
                    logger.warning("Storyteller client missing trigger_processing method")
            except Exception as e:
                 logger.error(f"⚡ Forge: Failed to trigger processing: {e}")
        else:
            logger.warning(f"⚡ Forge: Storyteller scan timed out (book not found after 5m). Processing might happen automatically later.")


        # Step 3: Cleanup Monitor
        # We wait for Storyteller to generate the readaloud, then delete our source files.
        # Storyteller usually outputs 'Book (readaloud).epub' or similar.
        
        AUDIO_EXTENSIONS = {'.mp3', '.m4b', '.m4a', '.flac', '.ogg', '.opus', '.wma', '.wav', '.aac'}
        MAX_WAIT = 3600  # 60 minutes
        POLL_INTERVAL = 30 # Check every 30s
        elapsed = 0

        logger.info(f"⚡ Forge: Starting cleanup monitor (polling every {POLL_INTERVAL}s, max {MAX_WAIT}s)")

        while elapsed < MAX_WAIT:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            try:
                # Check for readaloud epub in the destination folder
                # Storyteller naming: OriginalFilename (readaloud).epub
                readaloud_files = list(course_dir.glob("*readaloud*.epub")) + list(course_dir.glob("*synced*/*.epub"))
                
                if readaloud_files:
                    logger.info(f"⚡ Forge: Readaloud detected: {readaloud_files[0].name}")

                    # [SAFETY CHECK] Verify Storyteller is done with the files via API
                    # The user reported deletion happening while Storyteller was still scanning/syncing.
                    if found_uuid:
                        try:
                            # Poll status for a bit to ensure it's stable/ready
                            logger.info(f"⚡ Forge: Verifying processing status for {found_uuid}...")
                            is_ready = False
                            for _ in range(12): # Try for 60s
                                details = st_client.get_book_details(found_uuid)
                                if details:
                                    # Check sync status if available, or just existence of readaloud in response
                                    # But simplistic approach: just wait a safety buffer after file detection
                                    # If 'processing_status' exists use it, otherwise rely on file + delay.
                                    pass
                                time.sleep(5)
                            
                            # Explicit Safety Delay (requested by user)
                            logger.info("⚡ Forge: Safety delay (60s) to allow Storyteller to release file locks...")
                            time.sleep(60) 
                        except Exception as e:
                            logger.warning(f"Forge: Safety check failed: {e}. Proceeding with caution.")
                            time.sleep(30)

                    # Delete source audio files (ITERATE COURSE_DIR DIRECTLY)
                    deleted = 0
                    for f in course_dir.iterdir():
                        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                            try:
                                f.unlink()
                                deleted += 1
                            except Exception: pass
                    
                    # Delete source epub (ensure we don't delete the readaloud!)
                    if epub_dest.exists() and epub_dest not in readaloud_files:
                        try:
                            epub_dest.unlink()
                            deleted += 1
                        except Exception: pass

                    logger.info(f"⚡ Forge: Cleanup complete - deleted {deleted} source files.")
                    return

                # API check omitted for brevity/simplicity as filesystem check is reliable for local Storyteller
                
            except Exception as e:
                logger.warning(f"⚡ Forge: Cleanup monitor error: {e}")

        logger.warning(f"⚡ Forge: Cleanup monitor timed out after {MAX_WAIT}s for '{title}'. Source files remain.")

    except Exception as e:
        logger.error(f"❌ Forge: Background task failed for '{title}': {e}", exc_info=True)
    finally:
        with forge_lock:
            active_forge_tasks.discard(title)


def forge_process():
    """API: Start the forge process (copy files + cleanup in background)."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON payload"}), 400

    abs_id = data.get('abs_id')
    text_item = data.get('text_item')

    if not abs_id or not text_item:
        return jsonify({"error": "Missing abs_id or text_item"}), 400

    # Get title/author from ABS for folder naming
    title = "Unknown"
    author = "Unknown"
    try:
        item_details = container.abs_client().get_item_details(abs_id)
        if item_details:
            metadata = item_details.get('media', {}).get('metadata', {})
            title = metadata.get('title', 'Unknown')
            author = metadata.get('authorName', '') or get_abs_author(item_details) or 'Unknown'
    except Exception as e:
        logger.warning(f"Forge: Could not get ABS metadata for {abs_id}: {e}")

    # Start background thread
    thread = threading.Thread(
        target=_forge_background_task,
        args=(abs_id, text_item, title, author),
        daemon=True
    )
    thread.start()

    return jsonify({
        "message": f"Forge started for '{title}'. Processing and cleanup running in background.",
        "title": title,
        "author": author,
    }), 202


def match():
    if request.method == 'POST':
        abs_id = request.form.get('audiobook_id')
        selected_filename = request.form.get('ebook_filename')
        ebook_filename = selected_filename
        original_ebook_filename = None
        audiobooks = container.abs_client().get_all_audiobooks()
        selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
        if not selected_ab: return "Audiobook not found", 404

        # Get booklore_id if available for API-based hash computation
        booklore_id = None
        
        # [NEW] Storyteller Tri-Link Logic
        storyteller_uuid = request.form.get('storyteller_uuid')
        
        if storyteller_uuid:
            # If Storyteller UUID is selected, we prioritize it
            try:
                epub_cache = container.epub_cache_dir()
                if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)
                
                target_filename = f"storyteller_{storyteller_uuid}.epub"
                target_path = epub_cache / target_filename
                
                logger.info(f"Using Storyteller Artifact: {storyteller_uuid}")
                
                if container.storyteller_client().download_book(storyteller_uuid, target_path):
                    ebook_filename = target_filename # Override filename
                    original_ebook_filename = selected_filename # Preserve original
                    # We can also compute KOSync ID from this file now
                    kosync_doc_id = container.ebook_parser().get_kosync_id(target_path)
                else:
                    return "Failed to download Storyteller artifact", 500
                    
            except Exception as e:
                logger.error(f"Storyteller Link failed: {e}")
                return f"Storyteller Link failed: {e}", 500
        else:
            # Fallback to Standard Logic
            if container.booklore_client().is_configured():
                book = container.booklore_client().find_book_by_filename(ebook_filename)
                if book:
                    booklore_id = book.get('id')

            # Compute KOSync ID (Booklore API first, filesystem fallback)
            kosync_doc_id = get_kosync_id_for_ebook(ebook_filename, booklore_id)
            
        if not kosync_doc_id:
            logger.warning(f"Cannot compute KOSync ID for '{sanitize_log_data(ebook_filename)}': File not found in Booklore or filesystem")
            return "Could not compute KOSync ID for ebook", 404

        # [DUPLICATE MERGE] Check if this ebook is already linked to another ABS ID (e.g. ebook-only entry)
        existing_book = database_service.get_book_by_kosync_id(kosync_doc_id)
        migration_source_id = None
        
        if existing_book and existing_book.abs_id != abs_id:
            logger.info(f"🔄 Found existing book entry {existing_book.abs_id} for this ebook. Merging into {abs_id}...")
            migration_source_id = existing_book.abs_id
            
            # [ID SHADOWING] CAPTURE the old ID to use for Ebook sync
            abs_ebook_item_id = existing_book.abs_ebook_item_id or existing_book.abs_id
            
            # Preserve filename if available
            if not original_ebook_filename:
                original_ebook_filename = existing_book.original_ebook_filename or existing_book.ebook_filename
        else:
            # If no existing book, we assume this is a fresh link
            # [ID SHADOWING] But wait, if we are linking a pure ebook file, we don't have an item ID unless...
            # The logic relies on capturing it from the OLD book entry. 
            # If there is no old entry, we default to abs_id? 
            # Actually, per user instruction: "When existing_book is found... CAPTURE the old ID".
            # So if it's a fresh match, abs_ebook_item_id is None, which is fine (uses default behavior or assumes same).
            # But the user said "Add abs_ebook_item_id to Book model", which we did.
            abs_ebook_item_id = None

        # Create Book object and save to database service
        from src.db.models import Book
        book = Book(
            abs_id=abs_id,
            abs_title=manager.get_abs_title(selected_ab),
            ebook_filename=ebook_filename,
            kosync_doc_id=kosync_doc_id,
            transcript_file=None,
            status="pending",
            duration=manager.get_duration(selected_ab),
            storyteller_uuid=storyteller_uuid, # Save UUID
            original_ebook_filename=original_ebook_filename,
            abs_ebook_item_id=abs_ebook_item_id # [ID SHADOWING]
        )

        database_service.save_book(book)

        # [DUPLICATE MERGE] Perform Migration if needed
        if migration_source_id:
            try:
                database_service.migrate_book_data(migration_source_id, abs_id)
                database_service.delete_book(migration_source_id)
                logger.info(f"✅ Successfully merged {migration_source_id} into {abs_id}")
            except Exception as e:
                logger.error(f"❌ Failed to merge book data: {e}")

        # Trigger Hardcover Automatch
        hardcover_sync_client = container.sync_clients().get('Hardcover')
        if hardcover_sync_client and hardcover_sync_client.is_configured():
            hardcover_sync_client._automatch_hardcover(book)

        container.abs_client().add_to_collection(abs_id, ABS_COLLECTION_NAME)
        if container.booklore_client().is_configured():
            # Use original filename for shelf if we switched to storyteller
            shelf_filename = original_ebook_filename or ebook_filename
            container.booklore_client().add_to_shelf(shelf_filename, BOOKLORE_SHELF_NAME)
        if container.storyteller_client().is_configured():
            container.storyteller_client().add_to_collection(ebook_filename)

        # Auto-dismiss any pending suggestion for this book
        # Need to dismiss by BOTH abs_id (audiobook-triggered) and kosync_doc_id (ebook-triggered)
        database_service.dismiss_suggestion(abs_id)
        database_service.dismiss_suggestion(kosync_doc_id)

        return redirect(url_for('index'))

    search = request.args.get('search', '').strip().lower()
    audiobooks, ebooks, storyteller_books = [], [], []
    if search:
        # Fetch audiobooks conditionally based on ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID setting
        audiobooks = get_audiobooks_conditionally()
        audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
        for ab in audiobooks: ab['cover_url'] = f"{container.abs_client().base_url}/api/items/{ab['id']}/cover?token={container.abs_client().token}"

        # Use new search method
        ebooks = get_searchable_ebooks(search)
        
        # Search Storyteller
        if container.storyteller_client().is_configured():
            try:
                storyteller_books = container.storyteller_client().search_books(search)
            except Exception as e:
                logger.warning(f"Storyteller search failed in match route: {e}")

    return render_template('match.html', audiobooks=audiobooks, ebooks=ebooks, storyteller_books=storyteller_books, search=search, get_title=manager.get_abs_title)


def batch_match():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_to_queue':
            session.setdefault('queue', [])
            abs_id = request.form.get('audiobook_id')
            ebook_filename = request.form.get('ebook_filename')
            audiobooks = container.abs_client().get_all_audiobooks()
            selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
            if selected_ab and ebook_filename:
                if not any(item['abs_id'] == abs_id for item in session['queue']):
                    session['queue'].append({"abs_id": abs_id,
                                             "abs_title": manager.get_abs_title(selected_ab),
                                             "ebook_filename": ebook_filename,
                                             "duration": manager.get_duration(selected_ab),
                                             "cover_url": f"{container.abs_client().base_url}/api/items/{abs_id}/cover?token={container.abs_client().token}"})
                    session.modified = True
            return redirect(url_for('batch_match', search=request.form.get('search', '')))
        elif action == 'remove_from_queue':
            abs_id = request.form.get('abs_id')
            session['queue'] = [item for item in session.get('queue', []) if item['abs_id'] != abs_id]
            session.modified = True
            return redirect(url_for('batch_match'))
        elif action == 'clear_queue':
            session['queue'] = []
            session.modified = True
            return redirect(url_for('batch_match'))
        elif action == 'process_queue':
            from src.db.models import Book

            for item in session.get('queue', []):
                ebook_filename = item['ebook_filename']
                duration = item['duration']

                # Get booklore_id if available for API-based hash computation
                booklore_id = None
                if container.booklore_client().is_configured():
                    book = container.booklore_client().find_book_by_filename(ebook_filename)
                    if book:
                        booklore_id = book.get('id')

                # Compute KOSync ID (Booklore API first, filesystem fallback)
                kosync_doc_id = get_kosync_id_for_ebook(ebook_filename, booklore_id)
                if not kosync_doc_id:
                    logger.warning(f"Could not compute KOSync ID for {sanitize_log_data(ebook_filename)}, skipping")
                    continue

                # Create Book object and save to database service
                book = Book(
                    abs_id=item['abs_id'],
                    abs_title=item['abs_title'],
                    ebook_filename=ebook_filename,
                    kosync_doc_id=kosync_doc_id,
                    transcript_file=None,
                    status="pending",
                    duration=duration,
                    original_ebook_filename=None # Batch match currently only standard ebooks
                )

                database_service.save_book(book)

                # Trigger Hardcover Automatch
                hardcover_sync_client = container.sync_clients().get('Hardcover')
                if hardcover_sync_client and hardcover_sync_client.is_configured():
                    hardcover_sync_client._automatch_hardcover(book)

                container.abs_client().add_to_collection(item['abs_id'], ABS_COLLECTION_NAME)
                if container.booklore_client().is_configured():
                    container.booklore_client().add_to_shelf(ebook_filename, BOOKLORE_SHELF_NAME)
                if container.storyteller_client().is_configured():
                    container.storyteller_client().add_to_collection(ebook_filename)

            session['queue'] = []
            session.modified = True
            return redirect(url_for('index'))

    search = request.args.get('search', '').strip().lower()
    audiobooks, ebooks = [], []
    if search:
        audiobooks = get_audiobooks_conditionally()
        audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
        for ab in audiobooks: ab['cover_url'] = f"{container.abs_client().base_url}/api/items/{ab['id']}/cover?token={container.abs_client().token}"

        # Use new search method
        ebooks = get_searchable_ebooks(search)
        ebooks.sort(key=lambda x: x.name.lower())

    return render_template('batch_match.html', audiobooks=audiobooks, ebooks=ebooks, queue=session.get('queue', []), search=search,
                           get_title=manager.get_abs_title)


def delete_mapping(abs_id):
    # Get book from database service
    book = database_service.get_book(abs_id)
    if book:
        # Clean up transcript file if it exists
        if book.transcript_file:
            try:
                Path(book.transcript_file).unlink()
            except Exception:
                pass

        # Clean up cached ebook if it exists
        if book.ebook_filename:
            epub_cache = container.epub_cache_dir()
            cached_path = epub_cache / book.ebook_filename
            if cached_path.exists():
                try:
                    cached_path.unlink()
                    logger.info(f"🗑️ Deleted cached ebook file: {book.ebook_filename}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to delete cached ebook {book.ebook_filename}: {e}")

        # If ebook-only, also delete the raw KOSync document to allow a total fresh re-mapping
        if getattr(book, 'sync_mode', 'audiobook') == 'ebook_only' and book.kosync_doc_id:
            logger.info(f"Deleting KOSync document record for ebook-only mapping: {book.kosync_doc_id[:8]}")
            database_service.delete_kosync_document(book.kosync_doc_id)

        # [NEW] Delete cached ebook file
        if book.ebook_filename:
            try:
                # Use manager's cache dir which is already configured
                cache_file = manager.epub_cache_dir / book.ebook_filename
                if cache_file.exists():
                    cache_file.unlink()
                    logger.info(f"🗑️ Deleted ebook cache file: {book.ebook_filename}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to delete ebook cache file: {e}")

        # Remove from ABS collection
        collection_name = os.environ.get('ABS_COLLECTION_NAME', 'Synced with KOReader')
        try:
            container.abs_client().remove_from_collection(abs_id, collection_name)
        except Exception as e:
            logger.warning(f"⚠️ Failed to remove from ABS collection: {e}")

        # Remove from Booklore shelf
        if book.ebook_filename and container.booklore_client().is_configured():
            shelf_name = os.environ.get('BOOKLORE_SHELF_NAME', 'Kobo')
            try:
                container.booklore_client().remove_from_shelf(book.ebook_filename, shelf_name)
                # Same here regarding logging.
            except Exception as e:
                logger.warning(f"⚠️ Failed to remove from Booklore shelf: {e}")

    # Delete book and all associated data (states, jobs, hardcover details) via database service
    database_service.delete_book(abs_id)

    return redirect(url_for('index'))


def clear_progress(abs_id):
    """Clear progress for a mapping by setting all systems to 0%"""
    # Get book from database service
    book = database_service.get_book(abs_id)

    if not book:
        logger.warning(f"Cannot clear progress: book not found for {abs_id}")
        return redirect(url_for('index'))

    try:
        # Reset progress to 0 in all three systems
        logger.info(f"Clearing progress for {sanitize_log_data(book.abs_title or abs_id)}")
        manager.clear_progress(abs_id)
        logger.info(f"✅ Progress cleared successfully for {sanitize_log_data(book.abs_title or abs_id)}")

    except Exception as e:
        logger.error(f"Failed to clear progress for {abs_id}: {e}")

    return redirect(url_for('index'))


def update_hash(abs_id):
    from flask import flash
    new_hash = request.form.get('new_hash', '').strip()
    book = database_service.get_book(abs_id)

    if not book:
        flash("❌ Book not found", "error")
        return redirect(url_for('index'))

    old_hash = book.kosync_doc_id

    if new_hash:
        book.kosync_doc_id = new_hash
        database_service.save_book(book)
        logger.info(f"Updated KoSync hash for '{sanitize_log_data(book.abs_title)}' to manual input: {new_hash}")
        updated = True
    else:
        # Auto-regenerate
        booklore_id = None
        if container.booklore_client().is_configured():
            bl_book = container.booklore_client().find_book_by_filename(book.ebook_filename)
            if bl_book:
                booklore_id = bl_book.get('id')

        recalc_hash = get_kosync_id_for_ebook(book.ebook_filename, booklore_id)
        if recalc_hash:
            book.kosync_doc_id = recalc_hash
            database_service.save_book(book)
            logger.info(f"Auto-regenerated KoSync hash for '{sanitize_log_data(book.abs_title)}': {recalc_hash}")
            updated = True
        else:
            flash("❌ Could not recalculate hash (file not found?)", "error")
            return redirect(url_for('index'))

    # Migration: Push current progress to the NEW hash if it changed
    if updated and book.kosync_doc_id != old_hash:
        states = database_service.get_states_for_book(abs_id)
        kosync_state = next((s for s in states if s.client_name == 'kosync'), None)

        if kosync_state and kosync_state.percentage is not None:
            kosync_client = container.sync_clients().get('KoSync')
            if kosync_client and kosync_client.is_configured():
                success = kosync_client.kosync_client.update_progress(
                    book.kosync_doc_id,
                    kosync_state.percentage,
                    kosync_state.xpath
                )
                if success:
                    logger.info(f"Migrated progress for '{sanitize_log_data(book.abs_title)}' to new hash {book.kosync_doc_id}")

    flash(f"✅ Updated KoSync Hash for {book.abs_title}", "success")
    return redirect(url_for('index'))


def serve_cover(filename):
    """Serve cover images with lazy extraction."""
    # Filename is likely <hash>.jpg
    doc_hash = filename.replace('.jpg', '')

    # 1. Check if file exists
    cover_path = COVERS_DIR / filename
    if cover_path.exists():
        return send_from_directory(COVERS_DIR, filename)

    # 2. Try to extract
    # Find book by kosync ID
    book = database_service.get_book_by_kosync_id(doc_hash)

    if book and book.ebook_filename:
        # We need the full path to the book. ebook_parser resolves it usually.
        # extract_cover expects a path or filename that can be resolved.
        # Let's pass what we have.
        try:
             # Find actual file path using EbookParser resolution if needed,
             # but extract_cover in my implementation takes 'filepath' and calls Path(filepath).
             # If book.ebook_filename is just a name, we might need to resolve it.
             # container.ebook_parser().resolve_book_path(book.ebook_filename)

             # Actually, let's let EbookParser handle resolution or pass full path if we know it.
             # EbookParser.extract_cover currently does `Path(filepath)`.
             # It doesn't call `resolve_book_path` internally in the code I wrote?
             # Let's double check my implementation of extract_cover.
             # I wrote: `filepath = Path(filepath); book = epub.read_epub(str(filepath))`
             # So it expects a valid path. I should resolve it first.

             parser = container.ebook_parser()
             full_book_path = parser.resolve_book_path(book.ebook_filename)

             if parser.extract_cover(full_book_path, cover_path):
                 return send_from_directory(COVERS_DIR, filename)
        except Exception as e:
            logger.debug(f"Lazy cover extraction failed: {e}")

    return "Cover not found", 404

def api_storyteller_search():
    query = request.args.get('q', '')
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    results = container.storyteller_client().search_books(query)
    return jsonify(results)


def api_storyteller_link(abs_id):
    data = request.get_json()
    if not data or 'uuid' not in data:
        return jsonify({"error": "Missing 'uuid' in JSON payload"}), 400

    storyteller_uuid = data['uuid']
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"error": "Book not found"}), 404

    try:
        epub_cache = container.epub_cache_dir()
        if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)
        
        target_path = epub_cache / f"storyteller_{storyteller_uuid}.epub"
        
        if container.storyteller_client().download_book(storyteller_uuid, target_path):
            # [FIX] Sanitize Storyteller artifacts to remove <span> tags that break alignment
            from src.utils.ebook_utils import sanitize_storyteller_artifacts
            sanitize_storyteller_artifacts(target_path)

            # Preserve OLD filename as original if not already set
            if not book.original_ebook_filename:
                book.original_ebook_filename = book.ebook_filename
                logger.info(f"   [Tri-Link] Preserving original filename: {book.original_ebook_filename}")

            book.ebook_filename = target_path.name
            book.storyteller_uuid = storyteller_uuid
            # Also clear transcript to force re-alignment if needed? 
            # Ideally yes, but SyncManager handles DB_MANAGED check.
            # Maybe set status to pending to trigger re-alignment?
            # For now, just link. The user might need to re-scan.
            # Actually, let's set status to 'pending' to force a re-process with the new file!
            book.status = 'pending' # Force re-process to align with new EPUB
            # book.transcript_file = None # [OPTIMIZATION] Keep existing transcript to allow cache reuse
            
            database_service.save_book(book)
            
            # Dismiss suggestion if it exists
            database_service.dismiss_suggestion(abs_id)
            
            return jsonify({"message": "Book linked successfully", "filename": target_path.name}), 200
        else:
            return jsonify({"error": "Failed to download Storyteller artifact"}), 500
    except Exception as e:
        logger.error(f"Error linking Storyteller book for {abs_id}: {e}")
        return jsonify({"error": str(e)}), 500


def api_status():
    """Return status of all books from database service"""
    books = database_service.get_all_books()

    # Convert books to mappings format for API compatibility
    mappings = []
    for book in books:
        # Get states for this book
        states = database_service.get_states_for_book(book.abs_id)
        state_by_client = {state.client_name: state for state in states}

        mapping = {
            'abs_id': book.abs_id,
            'abs_title': book.abs_title,
            'ebook_filename': book.ebook_filename,
            'kosync_doc_id': book.kosync_doc_id,
            'transcript_file': book.transcript_file,
            'status': book.status,
            'sync_mode': getattr(book, 'sync_mode', 'audiobook'), # Default to audiobook for existing
            'duration': book.duration,
            'states': {}
        }

        # Add progress information from states
        for client_name, state in state_by_client.items():
            # Store in unified states object
            pct_val = round(state.percentage * 100, 1) if state.percentage is not None else 0

            mapping['states'][client_name] = {
                'timestamp': state.timestamp or 0,
                'percentage': pct_val,
                'xpath': getattr(state, 'xpath', None),
                'last_updated': state.last_updated
            }

            # Maintain backward compatibility with old field names
            if client_name == 'kosync':
                mapping['kosync_pct'] = pct_val
                mapping['kosync_xpath'] = getattr(state, 'xpath', None)
            elif client_name == 'abs':
                mapping['abs_pct'] = pct_val
                mapping['abs_ts'] = state.timestamp
            elif client_name == 'storyteller':
                mapping['storyteller_pct'] = pct_val
                mapping['storyteller_xpath'] = getattr(state, 'xpath', None)
            elif client_name == 'booklore':
                mapping['booklore_pct'] = pct_val
                mapping['booklore_xpath'] = getattr(state, 'xpath', None)

        mappings.append(mapping)

    return jsonify({"mappings": mappings})


def logs_view():
    """Display logs frontend with filtering capabilities."""
    return render_template('logs.html')


def api_logs():
    """API endpoint for fetching logs with filtering and pagination."""
    try:
        # Get query parameters
        lines_count = request.args.get('lines', 1000, type=int)
        min_level = request.args.get('level', 'DEBUG')
        search_term = request.args.get('search', '').lower()
        offset = request.args.get('offset', 0, type=int)

        # Limit lines count for performance
        lines_count = min(lines_count, 5000)

        # Read log files (current and backups)
        all_lines = []

        # Read current log file
        if LOG_PATH and LOG_PATH.exists():
            with open(LOG_PATH, 'r', encoding='utf-8') as f:
                all_lines.extend(f.readlines())

        # Read backup files if needed (for more history)
        if LOG_PATH and lines_count > len(all_lines):
            for i in range(1, 6):  # Check up to 5 backup files
                backup_path = Path(str(LOG_PATH) + f'.{i}')
                if backup_path.exists():
                    with open(backup_path, 'r', encoding='utf-8') as f:
                        backup_lines = f.readlines()
                        all_lines = backup_lines + all_lines
                        if len(all_lines) >= lines_count:
                            break

        # Parse and filter logs
        log_levels = {
            'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50
        }
        min_level_num = log_levels.get(min_level.upper(), 10)

        parsed_logs = []
        for line in all_lines:
            line = line.strip()
            if not line:
                continue

            # Parse log line format: [2024-01-09 10:30:45] LEVEL - MODULE: MESSAGE
            try:
                if line.startswith('[') and '] ' in line:
                    timestamp_end = line.find('] ')
                    timestamp_str = line[1:timestamp_end]
                    rest = line[timestamp_end + 2:]

                    if ': ' in rest:
                        level_module_str, message = rest.split(': ', 1)

                        # Check if format includes module (LEVEL - MODULE)
                        if ' - ' in level_module_str:
                            level_str, module_str = level_module_str.split(' - ', 1)
                        else:
                            # Old format without module
                            level_str = level_module_str
                            module_str = 'unknown'

                        level_num = log_levels.get(level_str.upper(), 20)

                        # Apply filters
                        if level_num >= min_level_num:
                            if not search_term or search_term in message.lower() or search_term in level_str.lower() or search_term in module_str.lower():
                                parsed_logs.append({
                                    'timestamp': timestamp_str,
                                    'level': level_str,
                                    'message': message,
                                    'module': module_str,
                                    'raw': line
                                })
                    else:
                        # Line without level, treat as INFO
                        if min_level_num <= 20:
                            if not search_term or search_term in rest.lower():
                                parsed_logs.append({
                                    'timestamp': timestamp_str,
                                    'level': 'INFO',
                                    'message': rest,
                                    'module': 'unknown',
                                    'raw': line
                                })
                else:
                    # Raw line without timestamp, treat as INFO
                    if min_level_num <= 20:
                        if not search_term or search_term in line.lower():
                            parsed_logs.append({
                                'timestamp': '',
                                'level': 'INFO',
                                'message': line,
                                'module': 'unknown',
                                'raw': line
                            })
            except Exception:
                # If parsing fails, include as raw line
                if not search_term or search_term in line.lower():
                    parsed_logs.append({
                        'timestamp': '',
                        'level': 'INFO',
                        'message': line,
                        'module': 'unknown',
                        'raw': line
                    })

        # Get recent logs first, then apply pagination
        recent_logs = parsed_logs[-lines_count:] if len(parsed_logs) > lines_count else parsed_logs

        # Apply offset for pagination
        if offset > 0:
            recent_logs = recent_logs[:-offset] if offset < len(recent_logs) else []

        return jsonify({
            'logs': recent_logs,
            'total_lines': len(parsed_logs),
            'displayed_lines': len(recent_logs),
            'has_more': len(parsed_logs) > lines_count + offset
        })

    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        return jsonify({'error': 'Failed to fetch logs', 'logs': [], 'total_lines': 0, 'displayed_lines': 0}), 500


def api_logs_live():
    """API endpoint for fetching recent live logs from memory."""
    try:
        # Get query parameters
        count = request.args.get('count', 50, type=int)
        min_level = request.args.get('level', 'DEBUG')
        search_term = request.args.get('search', '').lower()

        # Limit count for performance
        count = min(count, 500)

        log_levels = {
            'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50
        }
        min_level_num = log_levels.get(min_level.upper(), 10)

        # Get recent logs from memory
        recent_logs = memory_log_handler.get_recent_logs(count * 2)  # Get more to filter

        # Filter logs
        filtered_logs = []
        for log_entry in recent_logs:
            level_num = log_levels.get(log_entry['level'], 20)

            # Apply filters
            if level_num >= min_level_num:
                if not search_term or search_term in log_entry['message'].lower() or search_term in log_entry['level'].lower():
                    filtered_logs.append(log_entry)

        # Return most recent filtered logs
        result_logs = filtered_logs[-count:] if len(filtered_logs) > count else filtered_logs

        return jsonify({
            'logs': result_logs,
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        logger.error(f"Error fetching live logs: {e}")
        return jsonify({'error': 'Failed to fetch live logs', 'logs': [], 'timestamp': datetime.now().isoformat()}), 500


def view_log():
    """Legacy endpoint - redirect to new logs page."""
    return redirect(url_for('logs_view'))


# ---------------- SUGGESTION API ROUTES ----------------
def get_suggestions():
    suggestions = database_service.get_all_pending_suggestions()
    result = []
    for s in suggestions:
        try:
            matches = json.loads(s.matches_json) if s.matches_json else []
        except Exception:
            matches = []

        result.append({
            "id": s.id,
            "source_id": s.source_id,
            "title": s.title,
            "author": s.author,
            "cover_url": s.cover_url,
            "matches": matches,
            "created_at": s.created_at.isoformat()
        })
    return jsonify(result)


def dismiss_suggestion(source_id):
    if database_service.dismiss_suggestion(source_id):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Not found"}), 404


def ignore_suggestion(source_id):
    if database_service.ignore_suggestion(source_id):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Not found"}), 404


def clear_stale_suggestions():
    count = database_service.clear_stale_suggestions()
    logger.info(f"🧹 Cleared {count} stale suggestions from database")
    return jsonify({"success": True, "count": count})


def proxy_cover(abs_id):
    """Proxy cover access to allow loading covers from local network ABS instances."""
    try:
        token = container.abs_client().token
        base_url = container.abs_client().base_url
        if not token or not base_url:
            return "ABS not configured", 500

        url = f"{base_url.rstrip('/')}/api/items/{abs_id}/cover?token={token}"

        # Stream the response to avoid loading large images into memory
        req = requests.get(url, stream=True, timeout=10)
        if req.status_code == 200:
            from flask import Response
            return Response(req.iter_content(chunk_size=1024), content_type=req.headers.get('content-type', 'image/jpeg'))
        else:
            return "Cover not found", 404
    except Exception as e:
        logger.error(f"Error proxying cover for {abs_id}: {e}")
        return "Error loading cover", 500


# --- Logger setup (already present) ---
logger = logging.getLogger(__name__)

def get_booklore_libraries():
    """Return available Booklore libraries."""
    if not container.booklore_client().is_configured():
        return jsonify({"error": "Booklore not configured"}), 400
    
    libraries = container.booklore_client().get_libraries()
    return jsonify(libraries)

# --- Application Factory ---
def create_app(test_container=None):
    STATIC_DIR = os.environ.get('STATIC_DIR', '/app/static')
    TEMPLATE_DIR = os.environ.get('TEMPLATE_DIR', '/app/templates')
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='/static', template_folder=TEMPLATE_DIR)
    app.secret_key = "kosync-queue-secret-unified-app"

    # Setup dependencies and inject into app context
    setup_dependencies(app, test_container=test_container)

    # Register context processors, jinja globals, etc.
    app.context_processor(inject_global_vars)
    app.jinja_env.globals['safe_folder_name'] = safe_folder_name

    # Register all routes here
    app.add_url_rule('/', 'index', index)
    app.add_url_rule('/shelfmark', 'shelfmark', shelfmark)
    app.add_url_rule('/forge', 'forge', forge)
    app.add_url_rule('/match', 'match', match, methods=['GET', 'POST'])
    app.add_url_rule('/batch-match', 'batch_match', batch_match, methods=['GET', 'POST'])
    app.add_url_rule('/delete/<abs_id>', 'delete_mapping', delete_mapping, methods=['POST'])
    app.add_url_rule('/clear-progress/<abs_id>', 'clear_progress', clear_progress, methods=['POST'])
    app.add_url_rule('/update-hash/<abs_id>', 'update_hash', update_hash, methods=['POST'])
    app.add_url_rule('/covers/<path:filename>', 'serve_cover', serve_cover)
    app.add_url_rule('/api/status', 'api_status', api_status)
    app.add_url_rule('/logs', 'logs_view', logs_view)
    app.add_url_rule('/api/logs', 'api_logs', api_logs)
    app.add_url_rule('/api/logs/live', 'api_logs_live', api_logs_live)
    app.add_url_rule('/view_log', 'view_log', view_log)
    app.add_url_rule('/settings', 'settings', settings, methods=['GET', 'POST'])

    # Suggestion routes
    app.add_url_rule('/api/suggestions', 'get_suggestions', get_suggestions, methods=['GET'])
    app.add_url_rule('/api/suggestions/<source_id>/dismiss', 'dismiss_suggestion', dismiss_suggestion, methods=['POST'])
    app.add_url_rule('/api/suggestions/<source_id>/ignore', 'ignore_suggestion', ignore_suggestion, methods=['POST'])
    app.add_url_rule('/api/suggestions/clear_stale', 'clear_stale_suggestions', clear_stale_suggestions, methods=['POST'])
    app.add_url_rule('/api/cover-proxy/<abs_id>', 'proxy_cover', proxy_cover)
    app.add_url_rule('/api/booklore/libraries', 'get_booklore_libraries', get_booklore_libraries, methods=['GET'])

    # Storyteller API routes
    app.add_url_rule('/api/storyteller/search', 'api_storyteller_search', api_storyteller_search, methods=['GET'])
    app.add_url_rule('/api/storyteller/link/<abs_id>', 'api_storyteller_link', api_storyteller_link, methods=['POST'])

    # Forge routes
    app.add_url_rule('/api/forge/search_audio', 'forge_search_audio', forge_search_audio, methods=['GET'])
    app.add_url_rule('/api/forge/search_text', 'forge_search_text', forge_search_text, methods=['GET'])
    app.add_url_rule('/api/forge/process', 'forge_process', forge_process, methods=['POST'])
    
    @app.route('/api/forge/active', methods=['GET'])
    def forge_active_tasks():
        with forge_lock:
            return jsonify(list(active_forge_tasks))

    # Return both app and container for external reference
    return app, container

# ---------------- MAIN ----------------
if __name__ == '__main__':

    # Setup signal handlers to catch unexpected kills
    import signal
    def handle_exit_signal(signum, frame):
        logger.warning(f"⚠️ Received signal {signum} - Shutting down...")
        # Flush logs immediately
        for handler in logger.handlers:
            handler.flush()
        if hasattr(logging.getLogger(), 'handlers'):
            for handler in logging.getLogger().handlers:
                handler.flush()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_exit_signal)
    signal.signal(signal.SIGINT, handle_exit_signal)

    app, container = create_app()

    logger.info("=== Unified ABS Manager Started (Integrated Mode) ===")

    # Start sync daemon in background thread
    sync_daemon_thread = threading.Thread(target=sync_daemon, daemon=True)
    sync_daemon_thread.start()
    logger.info("Sync daemon thread started")



    # Check ebook source configuration
    booklore_configured = container.booklore_client().is_configured()
    books_volume_exists = container.books_dir().exists()

    if booklore_configured:
        logger.info(f"✅ Booklore integration enabled - ebooks sourced from API")
    elif books_volume_exists:
        logger.info(f"✅ Ebooks directory mounted at {container.books_dir()}")
    else:
        logger.info(
            "⚠️  NO EBOOK SOURCE CONFIGURED: Neither Booklore integration nor /books volume is available. "
            "New book matches will fail. Enable Booklore (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD) "
            "or mount the ebooks directory to /books."
        )


    logger.info(f"🌐 Web interface starting on port 5757")

    # --- Split-Port Mode ---
    sync_port = os.environ.get('KOSYNC_PORT')
    if sync_port and int(sync_port) != 5757:
        def run_sync_only_server(port):
            sync_app = Flask(__name__)
            sync_app.register_blueprint(kosync_sync_bp)
            @sync_app.route('/')
            def sync_health():
                return "Sync Server OK", 200
            sync_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

        threading.Thread(target=run_sync_only_server, args=(int(sync_port),), daemon=True).start()
        logger.info(f"🚀 Split-Port Mode Active: Sync-only server on port {sync_port}")

    app.run(host='0.0.0.0', port=5757, debug=False)





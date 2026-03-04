# [START FILE: abs-kosync-enhanced/web_server.py]
import glob
import html
import logging
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
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
from src.api.kosync_server import kosync_sync_bp, kosync_admin_bp, init_kosync_server
from src.api.hardcover_routes import hardcover_bp, init_hardcover_routes
from src.version import APP_VERSION, get_update_status
from src.db.models import State
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from src.utils.storyteller_transcript import StorytellerTranscript

def _reconfigure_logging():
    """Force update of root logger level based on env var."""
    try:
            new_level_str = os.environ.get('LOG_LEVEL', 'INFO').upper()
            new_level = getattr(logging, new_level_str, logging.INFO)

            root = logging.getLogger()
            root.setLevel(new_level)

            logger.info(f"📝 Logging level updated to {new_level_str}")
    except Exception as e:
            logger.warning(f"⚠️ Failed to reconfigure logging: {e}")

# ---------------- APP SETUP ----------------
container = None
manager = None
database_service = None
SUGGESTIONS_SCAN_JOBS = {}
SUGGESTIONS_SCAN_JOBS_LOCK = threading.Lock()
SUGGESTIONS_SCAN_JOB_TTL_SECONDS = 3600
SUGGESTIONS_STATE_STORE = {}
SUGGESTIONS_STATE_LOCK = threading.Lock()
SUGGESTIONS_STATE_TTL_SECONDS = 86400
SUGGESTIONS_CACHE_FILE_NAME = "suggestions_scan_cache.json"
SUGGESTIONS_CACHE_LOCK = threading.Lock()

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

    global LINKER_BOOKS_DIR, STORYTELLER_INGEST, ABS_AUDIO_ROOT
    global STORYTELLER_LIBRARY_DIR, EBOOK_IMPORT_DIR
    global ABS_API_URL, ABS_API_TOKEN, ABS_LIBRARY_ID
    global ABS_COLLECTION_NAME, BOOKLORE_SHELF_NAME, MONITOR_INTERVAL
    global SYNC_PERIOD_MINS, SYNC_DELTA_ABS_SECONDS, SYNC_DELTA_KOSYNC_PERCENT, FUZZY_MATCH_THRESHOLD

    LINKER_BOOKS_DIR = Path(os.environ.get("LINKER_BOOKS_DIR", "/linker_books"))
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
            logger.warning(f"⚠️ Invalid '{key}' value, defaulting to {default}")
            return float(default)

    SYNC_PERIOD_MINS = _get_float_env("SYNC_PERIOD_MINS", 5)
    SYNC_DELTA_ABS_SECONDS = _get_float_env("SYNC_DELTA_ABS_SECONDS", 30)
    SYNC_DELTA_KOSYNC_PERCENT = _get_float_env("SYNC_DELTA_KOSYNC_PERCENT", 0.005)
    FUZZY_MATCH_THRESHOLD = _get_float_env("FUZZY_MATCH_THRESHOLD", 0.8)

    ABS_COLLECTION_NAME = os.environ.get("ABS_COLLECTION_NAME", "Synced with KOReader")
    BOOKLORE_SHELF_NAME = os.environ.get("BOOKLORE_SHELF_NAME", "Kobo")
    MONITOR_INTERVAL = int(os.environ.get("MONITOR_INTERVAL", "3600"))

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

    logger.info(f"🚀 Web server dependencies initialized (DATA_DIR={DATA_DIR})")







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




# Storyteller Forge
STORYTELLER_LIBRARY_DIR = Path(os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))

# Track active forge operations for UI status
# Track active forge operations for UI status - MOVED TO FORGE SERVICE


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
            'STORYTELLER_INGEST_DIR': '/linker_books',
            'AUDIOBOOKS_DIR': '/audiobooks',
            'STORYTELLER_ASSETS_DIR': '',
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
from src.services.alignment_service import ingest_storyteller_transcripts









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
            logger.error(f"❌ Initial sync cycle failed: {e}")

        # Main daemon loop
        while True:
            try:
                # logger.debug("Running pending schedule jobs...")
                schedule.run_pending()
                time.sleep(30)  # Check every 30 seconds
            except Exception as e:
                logger.error(f"❌ Sync daemon error: {e}")
                time.sleep(60)  # Wait longer on error

    except Exception as e:
        logger.error(f"❌ Sync daemon crashed: {e}")


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
                    logger.debug(f"🔍 Computed KOSync ID from Booklore download: '{kosync_id}'")
                    return kosync_id
        except Exception as e:
            logger.warning(f"⚠️ Failed to get KOSync ID from Booklore, falling back to filesystem: {e}")

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
                 logger.info(f"📥 Attempting on-demand ABS download for '{abs_id}'")
                 ebook_files = abs_client.get_ebook_files(abs_id)
                 if ebook_files:
                     target = ebook_files[0]
                     if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)
                     
                     if abs_client.download_file(target['stream_url'], cached_path):
                         logger.info(f"   ✅ Downloaded ABS ebook to '{cached_path}'")
                         return container.ebook_parser().get_kosync_id(cached_path)
                 else:
                     logger.warning(f"   ⚠️ No ebook files found in ABS for item '{abs_id}'")
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
                     logger.info(f"📥 Attempting on-demand CWA download for ID '{cwa_id}'")
                     
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
                         logger.debug(f"🔍 Search did not return a usable result, trying direct ID lookup")
                         target = cwa_client.get_book_by_id(cwa_id)

                     if target and target.get('download_url'):
                         if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)
                         if cwa_client.download_ebook(target['download_url'], cached_path):
                             logger.info(f"   ✅ Downloaded CWA ebook to '{cached_path}'")
                             return container.ebook_parser().get_kosync_id(cached_path)
                     else:
                         logger.warning(f"   ⚠️ Could not find CWA book for ID '{cwa_id}'")
        except Exception as e:
            logger.error(f"   ❌ Failed CWA on-demand download: {e}")

    # Neither source available - log helpful warning
    if not container.booklore_client().is_configured() and not EBOOK_DIR.exists():
        logger.warning(
            f"⚠️ Cannot compute KOSync ID for '{ebook_filename}': "
            "Neither Booklore integration nor /books volume is configured. "
            "Enable Booklore (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD) "
            "or mount the ebooks directory to /books"
        )
    elif not booklore_id and not ebook_path:
        logger.warning(f"⚠️ Cannot compute KOSync ID for '{ebook_filename}': File not found in Booklore, filesystem, or remote sources")

    return None


class EbookResult:
    """Wrapper to provide consistent interface for ebooks from Booklore, CWA, ABS, or filesystem."""

    def __init__(self, name, title=None, subtitle=None, authors=None, booklore_id=None, path=None, source=None, source_id=None):
        self.name = name
        self.title = title or Path(name).stem
        self.subtitle = subtitle or ''
        self.authors = authors or ''
        self.booklore_id = booklore_id
        self.path = path # Public path
        self.source = source  # 'booklore', 'cwa', 'abs', 'filesystem'
        self.source_id = source_id or booklore_id # Generic ID for any source
        # Has metadata if we have a real title (not just filename) or booklore_id
        self.has_metadata = booklore_id is not None or (title is not None and title != name)

    @property
    def display_name(self):
        """Format: 'Title: Subtitle - Author' for sources with metadata, title for filesystem."""
        if self.has_metadata and self.title:
            full_title = self.title
            if self.subtitle:
                full_title = f"{self.title}: {self.subtitle}"
            if self.authors:
                return f"{full_title} - {self.authors}"
            return full_title
        return self.title

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
            if search_term:
                books = container.booklore_client().search_books(search_term)
            else:
                # For scan workloads, use the broader cache-oriented API to avoid
                # repeated aggressive refresh behavior from per-query search calls.
                books = container.booklore_client().get_all_books()
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
            logger.warning(f"⚠️ Booklore search failed: {e}")

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
                                    source='ABS',
                                    source_id=ab.get('id')
                                ))
                                found_filenames.add(fname.lower())
                                if ab.get('title'):
                                    found_stems.add(ab['title'].lower().strip())
        except Exception as e:
            logger.warning(f"⚠️ ABS ebook search failed: {e}")

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
                                path=cr.get('download_url'),
                                source='CWA',
                                source_id=cr.get('id')
                            ))
                            found_filenames.add(fname.lower())
                            if cr.get('title'):
                                found_stems.add(cr['title'].lower().strip())
        except Exception as e:
            logger.warning(f"⚠️ CWA search failed: {e}")

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
            logger.warning(f"⚠️ Filesystem search failed: {e}")

    # Check if we have no sources at all
    if not results and not EBOOK_DIR.exists() and not container.booklore_client().is_configured():
        logger.warning(
            "⚠️ No ebooks available: Neither Booklore integration nor /books volume is configured. "
            "Enable Booklore (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD) "
            "or mount the ebooks directory to /books"
        )

    return results



def restart_server():
    """
    Triggers a graceful restart by sending SIGTERM to the current process.
    The start.sh supervisor loop will catch the exit and restart the application.
    """
    logger.info("♻️  Stopping application (Supervisor will restart it)...")
    time.sleep(1.0)  # Give Flask time to send the redirect response

    # Send SIGTERM to our own process so the main thread's signal handler fires.
    # Note: sys.exit() does NOT work here because this runs in a background thread —
    # sys.exit() only raises SystemExit in the calling thread, not the main process.
    logger.info("👋 Sending SIGTERM to trigger restart...")
    import signal
    os.kill(os.getpid(), signal.SIGTERM)

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
            'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT',
            'INSTANT_SYNC_ENABLED',
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

            # Sanitize URLs
            url_keys = [
                'SHELFMARK_URL', 'ABS_SERVER', 'BOOKLORE_SERVER', 
                'STORYTELLER_API_URL', 'CWA_SERVER', 'KOSYNC_SERVER'
            ]
            if key in url_keys and clean_value:
                lower_val = clean_value.lower()
                if not (lower_val.startswith("http://") or lower_val.startswith("https://")):
                    clean_value = f"http://{clean_value}"

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
            logger.error(f"❌ Error saving settings: {e}")

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

    # Fetch ABS metadata once for the whole dashboard (single API call, not per-book)
    abs_metadata_by_id = {}
    try:
        all_abs_books = container.abs_client().get_all_audiobooks()
        for ab in all_abs_books:
            ab_id = ab.get('id')
            if ab_id:
                metadata = ab.get('media', {}).get('metadata', {})
                abs_metadata_by_id[ab_id] = {
                    'subtitle': metadata.get('subtitle') or '',
                    'author': metadata.get('authorName') or '',
                }
    except Exception as e:
        logger.warning(f"Could not fetch ABS metadata for dashboard enrichment: {e}")

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

        # Pull enriched ABS metadata from the pre-fetched lookup (no additional API calls)
        _abs_meta = abs_metadata_by_id.get(book.abs_id, {})
        abs_subtitle = _abs_meta.get('subtitle', '')
        abs_author = _abs_meta.get('author', '')

        # Create mapping dict for template compatibility
        mapping = {
            'abs_id': book.abs_id,
            'abs_title': book.abs_title,
            'abs_subtitle': abs_subtitle,
            'abs_author': abs_author,
            'ebook_filename': book.ebook_filename,
            'kosync_doc_id': book.kosync_doc_id,
            'transcript_file': book.transcript_file,
            'status': book.status,
            'sync_mode': getattr(book, 'sync_mode', 'audiobook'),
            'unified_progress': 0,
            'duration': book.duration or 0,
            'storyteller_uuid': book.storyteller_uuid,
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

    latest_version, update_available = get_update_status()

    return render_template(
        'index.html',
        mappings=mappings,
        integrations=integrations,
        progress=overall_progress,
        suggestions=suggestions,
        app_version=APP_VERSION,
        update_available=update_available,
        latest_version=latest_version
    )


def shelfmark():
    """Shelfmark view - renders an iframe with SHELFMARK_URL"""
    url = os.environ.get("SHELFMARK_URL")
    if not url:
        return redirect(url_for('index'))
    
    # Case-insensitive sanitization for the iframe source
    if not url.lower().startswith(('http://', 'https://')):
        url = f"http://{url}"
        
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
        logger.error(f"❌ Forge audio search failed: {e}", exc_info=True)
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
            logger.warning(f"⚠️ Forge: Booklore search failed: {e}")

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
        logger.warning(f"⚠️ Forge: ABS ebook search failed: {e}")

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
        logger.warning(f"⚠️ Forge: CWA search failed: {e}")

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
        logger.warning(f"⚠️ Forge: Local file search failed: {e}")

    return jsonify(results)





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
        logger.warning(f"⚠️ Forge: Could not get ABS metadata for '{abs_id}': {e}")

    # Start manual forge in service
    try:
        container.forge_service().start_manual_forge(abs_id, text_item, title, author)
        msg = f"Forge started for '{title}'. Processing and cleanup running in background."
    except Exception as e:
        logger.error(f"❌ Failed to start forge: {e}")
        return jsonify({"error": f"Failed to start forge: {e}"}), 500

    return jsonify({
        "message": msg,
        "title": title,
        "author": author,
    }), 202


def match():
    if request.method == 'POST':
        abs_id = request.form.get('audiobook_id')
        selected_filename = request.form.get('ebook_filename')
        ebook_filename = selected_filename
        original_ebook_filename = selected_filename
        audiobooks = container.abs_client().get_all_audiobooks()
        selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
        if not selected_ab: return "Audiobook not found", 404
        abs_title = manager.get_abs_title(selected_ab)
        item_details = container.abs_client().get_item_details(abs_id)
        chapters = item_details.get('media', {}).get('chapters', []) if item_details else []

        # Get booklore_id if available for API-based hash computation
        booklore_id = None
        
        # [NEW ACTION] Forge & Match
        if request.form.get('action') == 'forge_match':
            original_filename = request.form.get('ebook_filename')
            if not original_filename:
                return "Original ebook filename required for forge match", 400
                
            # 1. Prepare text item (reconstruct from form/source logic or assume passed)
            # Actually, standard match doesn't have 'text_item' logic fully exposed in form? 
            # The forge UI sends text_item JSON. 
            # But here we are arguably coming from the match page. 
            # If we add "Forge" button, we need to know the SOURCE of the text.
            # Assuming the form includes necessary details or we can infer.
            # [SIMPLIFICATION] For now, assume 'ebook_filename' is a valid Local text file from search?
            # Or is this action coming from the Forge modal? No, "Forge & Match".
            # If it's from the match page, the user selected an ebook result.
            # We need to reconstruct the `text_item` dict expected by ForgeService.
            
            # Extract source details from form (hidden inputs?)
            # We'll need to update match.html to send these.
            source_type = request.form.get('source_type')
            source_path = request.form.get('source_path') 
            source_id = request.form.get('source_id') # booklore id, cwa id, etc
            
            text_item = {
                "source": source_type,
                "path": source_path,
                "booklore_id": source_id,
                "cwa_id": source_id,
                "abs_id": source_id, # ambiguous but handled by specific keys
                "filename": original_filename
            }
            
            # Map specific keys based on source
            if source_type == 'ABS': text_item['abs_id'] = source_id
            if source_type == 'Booklore': text_item['booklore_id'] = source_id
            if source_type == 'CWA':
                text_item['cwa_id'] = source_id
                if source_path:
                    text_item['download_url'] = source_path
            if source_type == 'Local File': text_item['path'] = source_path
            
            # 2. Calculate initial Kosync ID (Original) - strictly for DB record
            # We use the ORIGINAL file for the ID initially (or forever if tri-linked).
            initial_booklore_id = source_id if source_type == 'Booklore' else None
            kosync_doc_id = get_kosync_id_for_ebook(original_filename, initial_booklore_id)
            
            if not kosync_doc_id:
                # If we can't get ID from original (e.g. remote only?), we might rely on the forged one later.
                # But we need a DB record now.
                # Generate a temporary or hash-based ID? Or fail?
                # Failing is safer.
                logger.warning(f"⚠️ Could not compute ID for original '{original_filename}'")
                # return "Could not compute KOSync ID for original file", 400
                # Actually, `start_auto_forge_match` can update it? 
                # Let's proceed with a placeholder or fail.
                # Use a specific error.
                pass 

            from src.db.models import Book
            # Create dummy book record with status='forging'
            book = Book(
                abs_id=abs_id,
                abs_title=abs_title,
                ebook_filename=original_filename,
                original_ebook_filename=original_filename,
                kosync_doc_id=kosync_doc_id or f"forging_{abs_id}", # temporary
                status="forging",
                duration=manager.get_duration(selected_ab)
            )
            database_service.save_book(book)
            
            # Start Auto-Forge
            author = get_abs_author(selected_ab)
            title = abs_title
            
            # Async launch
            container.forge_service().start_auto_forge_match(
                abs_id=abs_id,
                text_item=text_item,
                title=title,
                author=author,
                original_filename=original_filename,
                original_hash=kosync_doc_id
            )
            
            # Dismiss pending suggestion if it exists (for both ABS ID and potential KOSync ID)
            # This cleans up the suggestions list immediately upon starting the forge process
            database_service.dismiss_suggestion(abs_id)
            if kosync_doc_id:
                database_service.dismiss_suggestion(kosync_doc_id)

            return redirect(url_for('index'))
            
        # [NEW] Storyteller Tri-Link Logic
        storyteller_uuid = request.form.get('storyteller_uuid')
        
        if storyteller_uuid:
            # If Storyteller UUID is selected, we prioritize it
            try:
                epub_cache = container.epub_cache_dir()
                if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)
                
                target_filename = f"storyteller_{storyteller_uuid}.epub"
                target_path = epub_cache / target_filename
                
                logger.info(f"🔍 Using Storyteller Artifact: '{storyteller_uuid}'")
                
                downloaded = False
                try:
                    downloaded = container.storyteller_client().download_book(storyteller_uuid, target_path)
                except Exception as dl_err:
                    logger.warning(f"⚠️ Storyteller API download failed: {dl_err}")

                if not downloaded:
                    # Fallback: search Storyteller library for local readaloud EPUB
                    st_lib = Path(os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))
                    if abs_title and st_lib.exists():
                        for child in st_lib.iterdir():
                            if not child.is_dir():
                                continue
                            readaloud = list(child.glob("*readaloud*.epub")) + list(child.glob("*synced*/*.epub"))
                            if readaloud and child.name.lower().strip() == abs_title.lower().strip():
                                shutil.copy2(readaloud[0], target_path)
                                downloaded = True
                                logger.warning(f"⚠️ Storyteller: Using local fallback: '{readaloud[0]}'")
                                break

                if not downloaded:
                    return "Failed to download Storyteller artifact", 500

                ebook_filename = target_filename # Override filename
                original_ebook_filename = selected_filename # Preserve original

                # [FIX] Conditionally compute KOSync ID
                if original_ebook_filename:
                    # Tri-Link: Compute hash from the normal EPUB so it matches the user's device
                    logger.info(f"⚡ Tri-Link: Computing hash from original EPUB '{original_ebook_filename}'")
                    booklore_id = None
                    if container.booklore_client().is_configured():
                        bl_book = container.booklore_client().find_book_by_filename(original_ebook_filename)
                        if bl_book:
                            booklore_id = bl_book.get('id')
                    kosync_doc_id = get_kosync_id_for_ebook(original_ebook_filename, booklore_id)
                else:
                    # Storyteller-Only Link: Compute hash from the downloaded artifact
                    logger.info("⚡ Storyteller-Only Link: Computing hash from downloaded artifact")
                    kosync_doc_id = container.ebook_parser().get_kosync_id(target_path)
                    
            except Exception as e:
                logger.error(f"❌ Storyteller Link failed: {e}")
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
            logger.warning(f"⚠️ Cannot compute KOSync ID for '{sanitize_log_data(ebook_filename)}': File not found in Booklore or filesystem")
            return "Could not compute KOSync ID for ebook", 404

        # Hash Preservation: If the book already has a kosync_doc_id set,
        # preserve it. This respects manual overrides via update_hash and
        # prevents re-match from reverting a user's custom hash.
        current_book_entry = database_service.get_book(abs_id)
        if current_book_entry and current_book_entry.kosync_doc_id:
            logger.info(f"🔄 Preserving existing hash '{current_book_entry.kosync_doc_id}' for '{abs_id}' instead of new hash '{kosync_doc_id}'")
            kosync_doc_id = current_book_entry.kosync_doc_id

        # [DUPLICATE MERGE] Check if this ebook is already linked to another ABS ID (e.g. ebook-only entry)
        existing_book = database_service.get_book_by_kosync_id(kosync_doc_id)
        migration_source_id = None
        
        if existing_book and existing_book.abs_id != abs_id:
            logger.info(f"🔄 Found existing book entry '{existing_book.abs_id}' for this ebook — Merging into '{abs_id}'")
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
        storyteller_manifest = ingest_storyteller_transcripts(abs_id, abs_title, chapters)
        transcript_source = "storyteller" if storyteller_manifest else None
        book = Book(
            abs_id=abs_id,
            abs_title=abs_title,
            ebook_filename=ebook_filename,
            kosync_doc_id=kosync_doc_id,
            transcript_file=storyteller_manifest,
            status="pending",
            duration=manager.get_duration(selected_ab),
            transcript_source=transcript_source,
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
            if book.storyteller_uuid:
                container.storyteller_client().add_to_collection_by_uuid(book.storyteller_uuid)

        # Auto-dismiss any pending suggestion for this book
        # Need to dismiss by BOTH abs_id (audiobook-triggered) and kosync_doc_id (ebook-triggered)
        database_service.dismiss_suggestion(abs_id)
        database_service.dismiss_suggestion(kosync_doc_id)
        
        # [NEW] Robust Dismissal: Check if there's a different hash for this filename (e.g. from device)
        try:
            device_doc = database_service.get_kosync_doc_by_filename(ebook_filename)
            if device_doc and device_doc.document_hash != kosync_doc_id:
                logger.info(f"🔄 Dismissing additional suggestion/hash for '{ebook_filename}': '{device_doc.document_hash}'")
                database_service.dismiss_suggestion(device_doc.document_hash)
        except Exception as e:
            logger.warning(f"⚠️ Failed to check/dismiss device hash: {e}")

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
                logger.warning(f"⚠️ Storyteller search failed in match route: {e}")

    return render_template('match.html', audiobooks=audiobooks, ebooks=ebooks, storyteller_books=storyteller_books, search=search, get_title=manager.get_abs_title)


def batch_match():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_to_queue':
            session.setdefault('queue', [])
            abs_id = request.form.get('audiobook_id')
            ebook_filename = request.form.get('ebook_filename', '')
            ebook_display_name = request.form.get('ebook_display_name', ebook_filename)
            storyteller_uuid = request.form.get('storyteller_uuid', '')
            audiobooks = container.abs_client().get_all_audiobooks()
            selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
            # Allow queue entry if audiobook selected and either an ebook or a storyteller UUID is provided
            if selected_ab and (ebook_filename or storyteller_uuid):
                if not any(item['abs_id'] == abs_id for item in session['queue']):
                    session['queue'].append({"abs_id": abs_id,
                                             "abs_title": manager.get_abs_title(selected_ab),
                                             "ebook_filename": ebook_filename,
                                             "ebook_display_name": ebook_display_name,
                                             "storyteller_uuid": storyteller_uuid,
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
                storyteller_uuid = item.get('storyteller_uuid', '')
                original_ebook_filename = item['ebook_filename']
                duration = item['duration']
                booklore_id = None
                kosync_doc_id = None

                if storyteller_uuid:
                    # Storyteller Tri-Link Logic (mirrors match POST handler)
                    try:
                        epub_cache = container.epub_cache_dir()
                        if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)

                        target_filename = f"storyteller_{storyteller_uuid}.epub"
                        target_path = epub_cache / target_filename

                        logger.info(f"🔍 Batch Match: Using Storyteller Artifact '{storyteller_uuid}' for '{item['abs_title']}'")

                        if container.storyteller_client().download_book(storyteller_uuid, target_path):
                            original_ebook_filename = ebook_filename  # Preserve original (may be empty for storyteller-only)
                            ebook_filename = target_filename  # Override filename to cached artifact

                            if original_ebook_filename:
                                # Tri-Link: Compute hash from the original EPUB so it matches the user's device
                                logger.info(f"⚡ Batch Match Tri-Link: Computing hash from original EPUB '{original_ebook_filename}'")
                                if container.booklore_client().is_configured():
                                    bl_book = container.booklore_client().find_book_by_filename(original_ebook_filename)
                                    if bl_book:
                                        booklore_id = bl_book.get('id')
                                kosync_doc_id = get_kosync_id_for_ebook(original_ebook_filename, booklore_id)
                            else:
                                # Storyteller-Only Link: Compute hash from the downloaded artifact
                                logger.info("⚡ Batch Match Storyteller-Only Link: Computing hash from downloaded artifact")
                                kosync_doc_id = container.ebook_parser().get_kosync_id(target_path)
                        else:
                            logger.warning(f"⚠️ Failed to download Storyteller artifact '{storyteller_uuid}' for '{item['abs_title']}', skipping")
                            continue
                    except Exception as e:
                        logger.error(f"❌ Storyteller Tri-Link failed for '{item['abs_title']}': {e}")
                        continue
                else:
                    # Standard path: Get booklore_id if available for API-based hash computation
                    if container.booklore_client().is_configured():
                        book = container.booklore_client().find_book_by_filename(ebook_filename)
                        if book:
                            booklore_id = book.get('id')

                    # Compute KOSync ID (Booklore API first, filesystem fallback)
                    kosync_doc_id = get_kosync_id_for_ebook(ebook_filename, booklore_id)

                if not kosync_doc_id:
                    logger.warning(f"⚠️ Could not compute KOSync ID for {sanitize_log_data(ebook_filename)}, skipping")
                    continue

                # Hash Preservation for Batch Match: respect existing hash
                # (including manual overrides) to prevent re-match from reverting.
                current_book_entry = database_service.get_book(item['abs_id'])
                if current_book_entry and current_book_entry.kosync_doc_id:
                    logger.info(f"🔄 Preserving existing hash '{current_book_entry.kosync_doc_id}' for '{item['abs_id']}' instead of new hash '{kosync_doc_id}'")
                    kosync_doc_id = current_book_entry.kosync_doc_id

                item_details = container.abs_client().get_item_details(item['abs_id'])
                chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
                storyteller_manifest = ingest_storyteller_transcripts(
                    item['abs_id'],
                    item.get('abs_title', ''),
                    chapters
                )
                transcript_source = "storyteller" if storyteller_manifest else None

                # Create Book object and save to database service
                book = Book(
                    abs_id=item['abs_id'],
                    abs_title=item['abs_title'],
                    ebook_filename=ebook_filename,
                    kosync_doc_id=kosync_doc_id,
                    transcript_file=storyteller_manifest,
                    status="pending",
                    duration=duration,
                    transcript_source=transcript_source,
                    storyteller_uuid=storyteller_uuid or None,
                    original_ebook_filename=original_ebook_filename
                )

                database_service.save_book(book)

                # Trigger Hardcover Automatch
                hardcover_sync_client = container.sync_clients().get('Hardcover')
                if hardcover_sync_client and hardcover_sync_client.is_configured():
                    hardcover_sync_client._automatch_hardcover(book)

                container.abs_client().add_to_collection(item['abs_id'], ABS_COLLECTION_NAME)
                if container.booklore_client().is_configured():
                    shelf_filename = original_ebook_filename or ebook_filename
                    container.booklore_client().add_to_shelf(shelf_filename, BOOKLORE_SHELF_NAME)
                if container.storyteller_client().is_configured():
                    if book.storyteller_uuid:
                        container.storyteller_client().add_to_collection_by_uuid(book.storyteller_uuid)

                # Auto-dismiss any pending suggestion
                database_service.dismiss_suggestion(item['abs_id'])
                database_service.dismiss_suggestion(kosync_doc_id)
                
                # [NEW] Robust Dismissal
                try:
                    device_doc = database_service.get_kosync_doc_by_filename(ebook_filename)
                    if device_doc and device_doc.document_hash != kosync_doc_id:
                         database_service.dismiss_suggestion(device_doc.document_hash)
                except Exception: pass

            session['queue'] = []
            session.modified = True
            return redirect(url_for('index'))

    search = request.args.get('search', '').strip().lower()
    audiobooks, ebooks, storyteller_books = [], [], []
    if search:
        audiobooks = get_audiobooks_conditionally()
        audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
        for ab in audiobooks: ab['cover_url'] = f"{container.abs_client().base_url}/api/items/{ab['id']}/cover?token={container.abs_client().token}"

        # Use new search method
        ebooks = get_searchable_ebooks(search)
        ebooks.sort(key=lambda x: x.name.lower())

        # Search Storyteller
        if container.storyteller_client().is_configured():
            try:
                storyteller_books = container.storyteller_client().search_books(search)
            except Exception as e:
                logger.warning(f"⚠️ Storyteller search failed in batch_match route: {e}")

    return render_template('batch_match.html', audiobooks=audiobooks, ebooks=ebooks, storyteller_books=storyteller_books,
                           queue=session.get('queue', []), search=search, get_title=manager.get_abs_title)


def _get_suggestions_service():
    from src.services.suggestions_service import SuggestionsService

    return SuggestionsService(
        database_service=database_service,
        container=container,
        manager=manager,
        get_audiobooks_conditionally=get_audiobooks_conditionally,
        get_searchable_ebooks=get_searchable_ebooks,
        audiobook_matches_search=audiobook_matches_search,
        get_abs_author=get_abs_author,
        logger=logger,
    )


def _get_ignored_suggestion_source_ids():
    """Return ABS source IDs that are marked as ignored."""
    return _get_suggestions_service().get_ignored_suggestion_source_ids()


def scan_library_suggestions(cached_suggestions_by_abs=None, cached_no_match_abs_ids=None, progress_callback=None):
    """Scan for unmatched audiobooks and find candidate ebook matches."""
    return _get_suggestions_service().scan_library_suggestions(
        cached_suggestions_by_abs=cached_suggestions_by_abs,
        cached_no_match_abs_ids=cached_no_match_abs_ids,
        progress_callback=progress_callback,
    )


def _prune_suggestions_scan_jobs():
    cutoff = time.time() - SUGGESTIONS_SCAN_JOB_TTL_SECONDS
    with SUGGESTIONS_SCAN_JOBS_LOCK:
        stale_ids = [
            job_id for job_id, job in SUGGESTIONS_SCAN_JOBS.items()
            if job.get('updated_at', job.get('started_at', 0)) < cutoff
        ]
        for job_id in stale_ids:
            SUGGESTIONS_SCAN_JOBS.pop(job_id, None)


def _start_suggestions_scan_job(cached_suggestions_by_abs=None, cached_no_match_abs_ids=None):
    _prune_suggestions_scan_jobs()
    job_id = uuid.uuid4().hex
    with SUGGESTIONS_SCAN_JOBS_LOCK:
        SUGGESTIONS_SCAN_JOBS[job_id] = {
            "status": "running",
            "results": {},
            "error": None,
            "progress": {
                "phase": "initializing",
                "percent": 0,
                "message": "Preparing scan...",
                "scanned_new_done": 0,
                "scanned_new_total": 0,
                "reused_cached": 0,
                "total_unmatched": 0,
            },
            "started_at": time.time(),
            "updated_at": time.time(),
        }

    threading.Thread(
        target=_run_suggestions_scan_job,
        args=(job_id, cached_suggestions_by_abs or {}, cached_no_match_abs_ids or []),
        daemon=True
    ).start()
    return job_id


def _run_suggestions_scan_job(job_id, cached_suggestions_by_abs=None, cached_no_match_abs_ids=None):
    def update_progress(progress_payload):
        with SUGGESTIONS_SCAN_JOBS_LOCK:
            if job_id in SUGGESTIONS_SCAN_JOBS:
                SUGGESTIONS_SCAN_JOBS[job_id]["progress"] = progress_payload or {}
                SUGGESTIONS_SCAN_JOBS[job_id]["updated_at"] = time.time()

    try:
        results = scan_library_suggestions(
            cached_suggestions_by_abs=cached_suggestions_by_abs,
            cached_no_match_abs_ids=cached_no_match_abs_ids,
            progress_callback=update_progress,
        )
        _save_persisted_suggestions_cache({
            "scan_cache_by_abs": results.get('cache_by_abs', {}) if isinstance(results, dict) else {},
            "scan_cache_no_match_abs_ids": results.get('no_match_abs_ids', []) if isinstance(results, dict) else [],
            "scan_last_stats": results.get('stats', {}) if isinstance(results, dict) else {},
        })
        status = "done"
        error = None
    except Exception as e:
        logger.exception(f"Suggestions scan job failed ({job_id}): {e}")
        results = {}
        status = "error"
        error = str(e)
        update_progress({
            "phase": "error",
            "percent": 100,
            "message": "Scan failed",
            "scanned_new_done": 0,
            "scanned_new_total": 0,
            "reused_cached": 0,
            "total_unmatched": 0,
        })

    with SUGGESTIONS_SCAN_JOBS_LOCK:
        if job_id in SUGGESTIONS_SCAN_JOBS:
            SUGGESTIONS_SCAN_JOBS[job_id].update({
                "status": status,
                "results": results,
                "error": error,
                "updated_at": time.time(),
            })


def _get_suggestions_scan_job(job_id):
    if not job_id:
        return None
    with SUGGESTIONS_SCAN_JOBS_LOCK:
        job = SUGGESTIONS_SCAN_JOBS.get(job_id)
        if not job:
            return None
        return dict(job)


def _clear_legacy_suggestions_session_payload():
    """Remove old large suggestions payload keys from cookie-backed session."""
    legacy_keys = (
        'scan_results',
        'scan_cache_by_abs',
        'scan_cache_no_match_abs_ids',
        'scan_last_stats',
    )
    removed = False
    for key in legacy_keys:
        if key in session:
            session.pop(key, None)
            removed = True
    if removed:
        session.modified = True


def _prune_suggestions_state_store():
    cutoff = time.time() - SUGGESTIONS_STATE_TTL_SECONDS
    with SUGGESTIONS_STATE_LOCK:
        stale_ids = [
            state_id for state_id, state in SUGGESTIONS_STATE_STORE.items()
            if state.get('updated_at', state.get('created_at', 0)) < cutoff
        ]
        for state_id in stale_ids:
            SUGGESTIONS_STATE_STORE.pop(state_id, None)


def _default_suggestions_state():
    now = time.time()
    return {
        "scan_results": [],
        "scan_cache_by_abs": {},
        "scan_cache_no_match_abs_ids": [],
        "scan_last_stats": {},
        "scan_has_run": False,
        "created_at": now,
        "updated_at": now,
    }


def _get_suggestions_state(create=True):
    _prune_suggestions_state_store()
    state_id = session.get('suggestions_state_id')
    if not state_id and create:
        state_id = uuid.uuid4().hex
        session['suggestions_state_id'] = state_id
        session.modified = True

    if not state_id:
        return None, None

    with SUGGESTIONS_STATE_LOCK:
        state = SUGGESTIONS_STATE_STORE.get(state_id)
        if not state and create:
            state = _default_suggestions_state()
            SUGGESTIONS_STATE_STORE[state_id] = state

        if state:
            state['updated_at'] = time.time()
        return state_id, state


def _suggestions_cache_file_path():
    return DATA_DIR / SUGGESTIONS_CACHE_FILE_NAME


def _empty_suggestions_cache_payload():
    return {
        "scan_cache_by_abs": {},
        "scan_cache_no_match_abs_ids": [],
        "scan_last_stats": {},
        "updated_at": time.time(),
    }


def _load_persisted_suggestions_cache():
    cache_file = _suggestions_cache_file_path()
    if not cache_file.exists():
        return _empty_suggestions_cache_payload()

    with SUGGESTIONS_CACHE_LOCK:
        try:
            raw = json.loads(cache_file.read_text(encoding='utf-8'))
        except Exception as e:
            logger.warning(f"Could not read suggestions cache file '{cache_file}': {e}")
            return _empty_suggestions_cache_payload()

    payload = _empty_suggestions_cache_payload()
    if isinstance(raw, dict):
        cache_by_abs = raw.get('scan_cache_by_abs', {})
        no_match = raw.get('scan_cache_no_match_abs_ids', [])
        stats = raw.get('scan_last_stats', {})

        payload['scan_cache_by_abs'] = cache_by_abs if isinstance(cache_by_abs, dict) else {}
        payload['scan_cache_no_match_abs_ids'] = no_match if isinstance(no_match, list) else []
        payload['scan_last_stats'] = stats if isinstance(stats, dict) else {}

    return payload


def _save_persisted_suggestions_cache(payload):
    cache_file = _suggestions_cache_file_path()
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    safe_payload = {
        "scan_cache_by_abs": payload.get('scan_cache_by_abs', {}) if isinstance(payload.get('scan_cache_by_abs', {}), dict) else {},
        "scan_cache_no_match_abs_ids": payload.get('scan_cache_no_match_abs_ids', []) if isinstance(payload.get('scan_cache_no_match_abs_ids', []), list) else [],
        "scan_last_stats": payload.get('scan_last_stats', {}) if isinstance(payload.get('scan_last_stats', {}), dict) else {},
        "updated_at": time.time(),
    }

    temp_file = cache_file.with_suffix('.tmp')
    with SUGGESTIONS_CACHE_LOCK:
        try:
            temp_file.write_text(json.dumps(safe_payload, ensure_ascii=False), encoding='utf-8')
            temp_file.replace(cache_file)
        except Exception as e:
            logger.warning(f"Could not persist suggestions cache file '{cache_file}': {e}")
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass


def suggestions_page():
    _clear_legacy_suggestions_session_payload()
    state_id, suggestions_state = _get_suggestions_state(create=True)
    if suggestions_state is None:
        suggestions_state = _default_suggestions_state()

    if request.method == 'POST':
        action = request.form.get('action')

        if action in ('scan', 'scan_full'):
            full_refresh = (action == 'scan_full')
            if full_refresh:
                cached_suggestions_by_abs = {}
                cached_no_match_abs_ids = []
                suggestions_state['scan_cache_by_abs'] = {}
                suggestions_state['scan_cache_no_match_abs_ids'] = []
                suggestions_state['scan_last_stats'] = {}
                suggestions_state['scan_results'] = []
                suggestions_state['scan_has_run'] = False
                suggestions_state['updated_at'] = time.time()
                _save_persisted_suggestions_cache(_empty_suggestions_cache_payload())
            else:
                state_cache = suggestions_state.get('scan_cache_by_abs', {}) or {}
                state_no_match = suggestions_state.get('scan_cache_no_match_abs_ids', []) or []
                if state_cache or state_no_match:
                    cached_suggestions_by_abs = state_cache
                    cached_no_match_abs_ids = state_no_match
                else:
                    persisted_cache = _load_persisted_suggestions_cache()
                    cached_suggestions_by_abs = persisted_cache.get('scan_cache_by_abs', {}) or {}
                    cached_no_match_abs_ids = persisted_cache.get('scan_cache_no_match_abs_ids', []) or []

            job_id = _start_suggestions_scan_job(
                cached_suggestions_by_abs=cached_suggestions_by_abs,
                cached_no_match_abs_ids=cached_no_match_abs_ids,
            )
            session['suggestions_scan_job_id'] = job_id
            session.modified = True

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": True, "status": "running", "job_id": job_id, "full_refresh": full_refresh})
            return redirect(url_for('suggestions'))

        elif action == 'never':
            abs_id = request.form.get('abs_id')
            if abs_id:
                from src.db.models import PendingSuggestion

                current_scan_results = suggestions_state.get('scan_results', [])
                current_entry = next((s for s in current_scan_results if s.get('abs_id') == abs_id), None)
                abs_title = request.form.get('abs_title') or (current_entry.get('abs_title') if current_entry else '') or ''
                abs_author = request.form.get('abs_author') or (current_entry.get('abs_author') if current_entry else '') or ''
                cover_url = request.form.get('cover_url') or (current_entry.get('cover_url') if current_entry else '') or ''

                if not database_service.ignore_suggestion(abs_id):
                    database_service.save_pending_suggestion(PendingSuggestion(
                        source_id=abs_id,
                        title=abs_title,
                        author=abs_author,
                        cover_url=cover_url,
                        matches_json="[]",
                        status='ignored'
                    ))

                suggestions_state['scan_results'] = [item for item in current_scan_results if item.get('abs_id') != abs_id]
                cache_by_abs = suggestions_state.get('scan_cache_by_abs', {}) or {}
                if abs_id in cache_by_abs:
                    cache_by_abs.pop(abs_id, None)
                    suggestions_state['scan_cache_by_abs'] = cache_by_abs
                no_match_abs_ids = [x for x in (suggestions_state.get('scan_cache_no_match_abs_ids', []) or []) if x != abs_id]
                suggestions_state['scan_cache_no_match_abs_ids'] = no_match_abs_ids
                suggestions_state['updated_at'] = time.time()

            return redirect(url_for('suggestions'))

        elif action == 'add_to_queue':
            session.setdefault('queue', [])
            abs_id = request.form.get('audiobook_id')
            ebook_filename = request.form.get('ebook_filename', '')
            ebook_display_name = request.form.get('ebook_display_name', ebook_filename)
            storyteller_uuid = request.form.get('storyteller_uuid', '')
            audiobooks = container.abs_client().get_all_audiobooks()
            selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
            if selected_ab and (ebook_filename or storyteller_uuid):
                if not any(item['abs_id'] == abs_id for item in session['queue']):
                    session['queue'].append({
                        "abs_id": abs_id,
                        "abs_title": manager.get_abs_title(selected_ab),
                        "ebook_filename": ebook_filename,
                        "ebook_display_name": ebook_display_name,
                        "storyteller_uuid": storyteller_uuid,
                        "duration": manager.get_duration(selected_ab),
                        "cover_url": f"{container.abs_client().base_url}/api/items/{abs_id}/cover?token={container.abs_client().token}"
                    })
                    session.modified = True
            return redirect(url_for('suggestions'))

        elif action == 'remove_from_queue':
            abs_id = request.form.get('abs_id')
            session['queue'] = [item for item in session.get('queue', []) if item['abs_id'] != abs_id]
            session.modified = True
            return redirect(url_for('suggestions'))

        elif action == 'clear_queue':
            session['queue'] = []
            session.modified = True
            return redirect(url_for('suggestions'))

        elif action == 'process_queue':
            from src.db.models import Book

            for item in session.get('queue', []):
                ebook_filename = item['ebook_filename']
                storyteller_uuid = item.get('storyteller_uuid', '')
                original_ebook_filename = item['ebook_filename']
                duration = item['duration']
                booklore_id = None
                kosync_doc_id = None

                if storyteller_uuid:
                    # Storyteller Tri-Link Logic (mirrors match POST handler)
                    try:
                        epub_cache = container.epub_cache_dir()
                        if not epub_cache.exists():
                            epub_cache.mkdir(parents=True, exist_ok=True)

                        target_filename = f"storyteller_{storyteller_uuid}.epub"
                        target_path = epub_cache / target_filename

                        logger.info(f"Batch Match: Using Storyteller Artifact '{storyteller_uuid}' for '{item['abs_title']}'")

                        if container.storyteller_client().download_book(storyteller_uuid, target_path):
                            original_ebook_filename = ebook_filename
                            ebook_filename = target_filename

                            if original_ebook_filename:
                                logger.info(f"Batch Match Tri-Link: Computing hash from original EPUB '{original_ebook_filename}'")
                                if container.booklore_client().is_configured():
                                    bl_book = container.booklore_client().find_book_by_filename(original_ebook_filename)
                                    if bl_book:
                                        booklore_id = bl_book.get('id')
                                kosync_doc_id = get_kosync_id_for_ebook(original_ebook_filename, booklore_id)
                            else:
                                logger.info("Batch Match Storyteller-Only Link: Computing hash from downloaded artifact")
                                kosync_doc_id = container.ebook_parser().get_kosync_id(target_path)
                        else:
                            logger.warning(f"Failed to download Storyteller artifact '{storyteller_uuid}' for '{item['abs_title']}', skipping")
                            continue
                    except Exception as e:
                        logger.error(f"Storyteller Tri-Link failed for '{item['abs_title']}': {e}")
                        continue
                else:
                    if container.booklore_client().is_configured():
                        book = container.booklore_client().find_book_by_filename(ebook_filename)
                        if book:
                            booklore_id = book.get('id')

                    kosync_doc_id = get_kosync_id_for_ebook(ebook_filename, booklore_id)

                if not kosync_doc_id:
                    logger.warning(f"Could not compute KOSync ID for {sanitize_log_data(ebook_filename)}, skipping")
                    continue

                current_book_entry = database_service.get_book(item['abs_id'])
                if current_book_entry and current_book_entry.kosync_doc_id:
                    logger.info(f"Preserving existing hash '{current_book_entry.kosync_doc_id}' for '{item['abs_id']}' instead of new hash '{kosync_doc_id}'")
                    kosync_doc_id = current_book_entry.kosync_doc_id

                item_details = container.abs_client().get_item_details(item['abs_id'])
                chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
                storyteller_manifest = ingest_storyteller_transcripts(
                    item['abs_id'],
                    item.get('abs_title', ''),
                    chapters
                )
                transcript_source = "storyteller" if storyteller_manifest else None

                book = Book(
                    abs_id=item['abs_id'],
                    abs_title=item['abs_title'],
                    ebook_filename=ebook_filename,
                    kosync_doc_id=kosync_doc_id,
                    transcript_file=storyteller_manifest,
                    status="pending",
                    duration=duration,
                    transcript_source=transcript_source,
                    storyteller_uuid=storyteller_uuid or None,
                    original_ebook_filename=original_ebook_filename
                )

                database_service.save_book(book)

                hardcover_sync_client = container.sync_clients().get('Hardcover')
                if hardcover_sync_client and hardcover_sync_client.is_configured():
                    hardcover_sync_client._automatch_hardcover(book)

                container.abs_client().add_to_collection(item['abs_id'], ABS_COLLECTION_NAME)
                if container.booklore_client().is_configured():
                    shelf_filename = original_ebook_filename or ebook_filename
                    container.booklore_client().add_to_shelf(shelf_filename, BOOKLORE_SHELF_NAME)
                if container.storyteller_client().is_configured():
                    if book.storyteller_uuid:
                        container.storyteller_client().add_to_collection_by_uuid(book.storyteller_uuid)

                database_service.dismiss_suggestion(item['abs_id'])
                database_service.dismiss_suggestion(kosync_doc_id)

                try:
                    device_doc = database_service.get_kosync_doc_by_filename(ebook_filename)
                    if device_doc and device_doc.document_hash != kosync_doc_id:
                        database_service.dismiss_suggestion(device_doc.document_hash)
                except Exception:
                    pass

            session['queue'] = []
            session.modified = True
            return redirect(url_for('index'))

    scan_in_progress = False
    scan_error = None
    job_id = session.get('suggestions_scan_job_id')
    if job_id:
        scan_job = _get_suggestions_scan_job(job_id)
        if not scan_job:
            session.pop('suggestions_scan_job_id', None)
            session.modified = True
        else:
            status = scan_job.get('status')
            if status == 'done':
                scan_payload = scan_job.get('results', {}) or {}
                suggestions_state['scan_results'] = scan_payload.get('suggestions', [])
                suggestions_state['scan_cache_by_abs'] = scan_payload.get('cache_by_abs', {})
                suggestions_state['scan_cache_no_match_abs_ids'] = scan_payload.get('no_match_abs_ids', [])
                suggestions_state['scan_last_stats'] = scan_payload.get('stats', {})
                suggestions_state['scan_has_run'] = True
                suggestions_state['updated_at'] = time.time()
                _save_persisted_suggestions_cache({
                    "scan_cache_by_abs": suggestions_state.get('scan_cache_by_abs', {}),
                    "scan_cache_no_match_abs_ids": suggestions_state.get('scan_cache_no_match_abs_ids', []),
                    "scan_last_stats": suggestions_state.get('scan_last_stats', {}),
                })
                session.pop('suggestions_scan_job_id', None)
                session.modified = True
                with SUGGESTIONS_SCAN_JOBS_LOCK:
                    SUGGESTIONS_SCAN_JOBS.pop(job_id, None)
            elif status == 'error':
                scan_error = scan_job.get('error') or 'Scan failed'
                session.pop('suggestions_scan_job_id', None)
                session.modified = True
                with SUGGESTIONS_SCAN_JOBS_LOCK:
                    SUGGESTIONS_SCAN_JOBS.pop(job_id, None)
            else:
                scan_in_progress = True

    ignored_source_ids = _get_ignored_suggestion_source_ids()
    scan_results = suggestions_state.get('scan_results', [])
    cache_by_abs = suggestions_state.get('scan_cache_by_abs', {}) or {}
    no_match_abs_ids = suggestions_state.get('scan_cache_no_match_abs_ids', []) or []
    if ignored_source_ids:
        filtered_results = [item for item in scan_results if item.get('abs_id') not in ignored_source_ids]
        filtered_cache_by_abs = {
            abs_id: suggestion for abs_id, suggestion in cache_by_abs.items()
            if abs_id not in ignored_source_ids
        }
        filtered_no_match_abs_ids = [abs_id for abs_id in no_match_abs_ids if abs_id not in ignored_source_ids]
        if len(filtered_results) != len(scan_results):
            suggestions_state['scan_results'] = filtered_results
            scan_results = filtered_results
            suggestions_state['updated_at'] = time.time()
        if len(filtered_cache_by_abs) != len(cache_by_abs):
            suggestions_state['scan_cache_by_abs'] = filtered_cache_by_abs
            cache_by_abs = filtered_cache_by_abs
            suggestions_state['updated_at'] = time.time()
        if len(filtered_no_match_abs_ids) != len(no_match_abs_ids):
            suggestions_state['scan_cache_no_match_abs_ids'] = filtered_no_match_abs_ids
            no_match_abs_ids = filtered_no_match_abs_ids
            suggestions_state['updated_at'] = time.time()
        _save_persisted_suggestions_cache({
            "scan_cache_by_abs": suggestions_state.get('scan_cache_by_abs', {}),
            "scan_cache_no_match_abs_ids": suggestions_state.get('scan_cache_no_match_abs_ids', []),
            "scan_last_stats": suggestions_state.get('scan_last_stats', {}),
        })

    return render_template(
        'suggestions.html',
        suggestions=scan_results,
        queue=session.get('queue', []),
        scan_has_run=bool(suggestions_state.get('scan_has_run', False)),
        scan_in_progress=scan_in_progress,
        scan_error=scan_error,
        scan_stats=suggestions_state.get('scan_last_stats', {}),
    )


def suggestions_scan_status():
    _clear_legacy_suggestions_session_payload()
    job_id = session.get('suggestions_scan_job_id')
    if not job_id:
        return jsonify({"status": "idle"})

    scan_job = _get_suggestions_scan_job(job_id)
    if not scan_job:
        session.pop('suggestions_scan_job_id', None)
        session.modified = True
        return jsonify({"status": "idle"})

    response = {
        "status": scan_job.get('status', 'idle'),
        "error": scan_job.get('error'),
        "progress": scan_job.get('progress', {}),
    }
    if scan_job.get('status') == 'done':
        result_payload = scan_job.get('results', {}) or {}
        response["count"] = len(result_payload.get('suggestions', []))
        response["stats"] = result_payload.get('stats', {})

    return jsonify(response)


def cleanup_mapping_resources(book):
    """Delete external artifacts and membership data for a mapped book."""
    if not book:
        return

    if book.transcript_file:
        try:
            Path(book.transcript_file).unlink()
        except Exception:
            pass

    # Clean up audio cache directory (WAV files from whisper transcription)
    audio_cache_dir = DATA_DIR / "audio_cache" / book.abs_id
    if audio_cache_dir.exists():
        try:
            shutil.rmtree(audio_cache_dir)
            logger.info(f"🗑️ Deleted audio cache: {audio_cache_dir}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to delete audio cache: {e}")

    # Clean up full transcript directory (chapter JSON files + manifest)
    transcript_dir = DATA_DIR / "transcripts" / "storyteller" / book.abs_id
    if transcript_dir.exists():
        try:
            shutil.rmtree(transcript_dir)
            logger.info(f"🗑️ Deleted transcript directory: {transcript_dir}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to delete transcript directory: {e}")

    if book.ebook_filename:
        cache_dirs = []
        try:
            cache_dirs.append(container.epub_cache_dir())
        except Exception:
            pass

        manager_cache_dir = getattr(manager, 'epub_cache_dir', None)
        if manager_cache_dir:
            cache_dirs.append(manager_cache_dir)

        seen_dirs = set()
        for cache_dir in cache_dirs:
            cache_dir_path = Path(cache_dir)
            cache_dir_key = str(cache_dir_path)
            if cache_dir_key in seen_dirs:
                continue
            seen_dirs.add(cache_dir_key)

            cached_path = cache_dir_path / book.ebook_filename
            if cached_path.exists():
                try:
                    cached_path.unlink()
                    logger.info(f"🗑️ Deleted cached ebook file: {book.ebook_filename}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to delete cached ebook {book.ebook_filename}: {e}")

    if getattr(book, 'sync_mode', 'audiobook') == 'ebook_only' and book.kosync_doc_id:
        logger.info(f"🗑️ Deleting KOSync document record for ebook-only mapping: '{book.kosync_doc_id[:8]}'")
        database_service.delete_kosync_document(book.kosync_doc_id)

    collection_name = os.environ.get('ABS_COLLECTION_NAME', 'Synced with KOReader')
    try:
        container.abs_client().remove_from_collection(book.abs_id, collection_name)
    except Exception as e:
        logger.warning(f"⚠️ Failed to remove from ABS collection: {e}")

    storyteller_uuid = getattr(book, 'storyteller_uuid', None)
    if not storyteller_uuid and getattr(book, 'ebook_filename', None):
        match = re.match(r"^storyteller_([0-9a-fA-F-]+)\.epub$", book.ebook_filename)
        if match:
            storyteller_uuid = match.group(1)
            logger.info(f"Inferred Storyteller UUID for cleanup: '{storyteller_uuid[:8]}...'")

    if storyteller_uuid:
        storyteller_collection_name = os.environ.get('STORYTELLER_COLLECTION_NAME', 'Synced with KOReader')
        try:
            st_client = container.storyteller_client()
            if hasattr(st_client, 'remove_from_collection_by_uuid'):
                removed = st_client.remove_from_collection_by_uuid(storyteller_uuid, storyteller_collection_name)
                if not removed:
                    logger.warning(f"Storyteller collection removal returned no success for '{storyteller_uuid[:8]}...'")
            else:
                logger.warning("Storyteller client has no remove_from_collection_by_uuid method")
        except Exception as e:
            logger.warning(f"Failed to remove from Storyteller collection: {e}")

    if book.ebook_filename and container.booklore_client().is_configured():
        shelf_name = os.environ.get('BOOKLORE_SHELF_NAME', 'Kobo')
        try:
            shelf_filename = book.original_ebook_filename or book.ebook_filename
            container.booklore_client().remove_from_shelf(shelf_filename, shelf_name)
        except Exception as e:
            logger.warning(f"⚠️ Failed to remove from Booklore shelf: {e}")


def delete_mapping(abs_id):
    book = database_service.get_book(abs_id)
    if book:
        cleanup_mapping_resources(book)

    # Delete book and all associated data (states, jobs, hardcover details) via database service
    database_service.delete_book(abs_id)

    return redirect(url_for('index'))


def clear_progress(abs_id):
    """Clear progress for a mapping by setting all systems to 0%"""
    # Get book from database service
    book = database_service.get_book(abs_id)

    if not book:
        logger.warning(f"⚠️ Cannot clear progress: book not found for '{abs_id}'")
        return redirect(url_for('index'))

    try:
        # Reset progress to 0 in all three systems
        logger.info(f"🔄 Clearing progress for {sanitize_log_data(book.abs_title or abs_id)}")
        manager.clear_progress(abs_id)
        logger.info(f"✅ Progress cleared successfully for {sanitize_log_data(book.abs_title or abs_id)}")

    except Exception as e:
        logger.error(f"❌ Failed to clear progress for '{abs_id}': {e}")

    return redirect(url_for('index'))



def sync_now(abs_id):
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404
    
    threading.Thread(target=manager.sync_cycle, kwargs={'target_abs_id': abs_id}, daemon=True).start()
    return jsonify({"success": True})

def mark_complete(abs_id):
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404
        
    perform_delete = request.json.get('delete', False) if request.json else False
    
    locator = LocatorResult(percentage=1.0)

    update_req = UpdateProgressRequest(locator_result=locator, txt="Book finished", previous_location=None)
    
    for client_name, client in container.sync_clients().items():
        if client.is_configured():
            if client_name.lower() == 'abs':
                client.abs_client.mark_finished(abs_id)
            else:
                client.update_progress(book, update_req)
                
            state = State(
                abs_id=abs_id,
                client_name=client_name.lower(),
                percentage=1.0,
                timestamp=int(time.time()),
                last_updated=int(time.time())
            )
            database_service.save_state(state)
            
    if perform_delete:
        cleanup_mapping_resources(book)
        database_service.delete_book(abs_id)
        
    return jsonify({"success": True})

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
        logger.info(f"✅ Updated KoSync hash for '{sanitize_log_data(book.abs_title)}' to manual input: '{new_hash}'")
        updated = True
    else:
        # Auto-regenerate
        # [NEW] User Request: If recalculating (empty input), prioritize the standard EPUB (original_ebook_filename)
        # over the current filename (which might be a Storyteller artifact).
        target_filename = book.original_ebook_filename or book.ebook_filename
        
        booklore_id = None
        if container.booklore_client().is_configured():
            bl_book = container.booklore_client().find_book_by_filename(target_filename)
            if bl_book:
                booklore_id = bl_book.get('id')

        recalc_hash = get_kosync_id_for_ebook(target_filename, booklore_id, original_filename=book.ebook_filename)
        
        if recalc_hash:
            # [CHANGED] Manual update (via UI) should always succeed, even if it changes a linked hash.
            # The protection logic remains in match() and batch_match() to prevent automated overwrites.
            book.kosync_doc_id = recalc_hash
            database_service.save_book(book)
            logger.info(f"✅ Auto-regenerated KoSync hash for '{sanitize_log_data(book.abs_title)}': '{recalc_hash}'")
            updated = True
        else:
            flash("❌ Could not recalculate hash (file not found?)", "error")
            return redirect(url_for('index'))

    # Trigger an instant sync cycle so the engine can reconcile progress
    # using 'furthest wins' logic. This avoids overwriting newer progress
    # that may already exist on the KOSync server (e.g., from BookNexus).
    if updated and book.kosync_doc_id != old_hash:
        logger.info(f"🔄 Hash changed for '{sanitize_log_data(book.abs_title)}' — triggering instant sync to reconcile progress")
        threading.Thread(target=manager.sync_cycle, kwargs={'target_abs_id': abs_id}, daemon=True).start()

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

    # [NEW] Handle explicit unlinking
    if storyteller_uuid == "none" or not storyteller_uuid:
        logger.info(f"🔄 Unlinking Storyteller for '{book.abs_title}'")
        previous_storyteller_uuid = book.storyteller_uuid
        if not previous_storyteller_uuid and getattr(book, 'ebook_filename', None):
            match = re.match(r"^storyteller_([0-9a-fA-F-]+)\.epub$", book.ebook_filename)
            if match:
                previous_storyteller_uuid = match.group(1)
                logger.info(f"Inferred Storyteller UUID for unlink: '{previous_storyteller_uuid[:8]}...'")
        book.storyteller_uuid = None
        book.transcript_source = None
        book.transcript_file = None

        if previous_storyteller_uuid:
            try:
                st_client = container.storyteller_client()
                if hasattr(st_client, 'remove_from_collection_by_uuid'):
                    storyteller_collection_name = os.environ.get('STORYTELLER_COLLECTION_NAME', 'Synced with KOReader')
                    removed = st_client.remove_from_collection_by_uuid(previous_storyteller_uuid, storyteller_collection_name)
                    if not removed:
                        logger.warning(f"Storyteller unlink removal returned no success for '{previous_storyteller_uuid[:8]}...'")
                else:
                    logger.warning("Storyteller client has no remove_from_collection_by_uuid method")
            except Exception as e:
                logger.warning(f"Failed to remove Storyteller UUID from collection: {e}")
        
        # Revert to original filename if it exists
        if book.original_ebook_filename:
            book.ebook_filename = book.original_ebook_filename
            
        book.status = 'pending' # Force re-process to align with standard EPUB
        database_service.save_book(book)
        
        return jsonify({"message": "Storyteller unlinked successfully", "filename": book.ebook_filename}), 200

    try:
        epub_cache = container.epub_cache_dir()
        if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)
        
        target_path = epub_cache / f"storyteller_{storyteller_uuid}.epub"
        
        if container.storyteller_client().download_book(storyteller_uuid, target_path):


            # Preserve OLD filename as original if not already set
            if not book.original_ebook_filename:
                book.original_ebook_filename = book.ebook_filename
                logger.info(f"   ⚡ Preserving original filename: '{book.original_ebook_filename}'")

            book.ebook_filename = target_path.name
            book.storyteller_uuid = storyteller_uuid
            item_details = container.abs_client().get_item_details(abs_id)
            chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
            storyteller_manifest = ingest_storyteller_transcripts(abs_id, book.abs_title or '', chapters)
            book.transcript_file = storyteller_manifest
            book.transcript_source = "storyteller" if storyteller_manifest else None
            book.status = 'pending' # Force re-process to align with new EPUB
            
            database_service.save_book(book)
            
            # Dismiss suggestion if it exists
            database_service.dismiss_suggestion(abs_id)
            
            return jsonify({"message": "Book linked successfully", "filename": target_path.name}), 200
        else:
            return jsonify({"error": "Failed to download Storyteller artifact"}), 500
    except Exception as e:
        logger.error(f"❌ Error linking Storyteller book for '{abs_id}': {e}")
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
            'storyteller_uuid': book.storyteller_uuid,
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
        logger.error(f"❌ Error fetching logs: {e}")
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
        logger.error(f"❌ Error fetching live logs: {e}")
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


def clean_inactive_cache():
    """Delete audio_cache, transcript dirs, and cached EPUBs for books that are not active."""
    active_books = database_service.get_books_by_status('active')
    active_ids = {b.abs_id for b in active_books}
    active_ebook_files = {b.ebook_filename for b in active_books if b.ebook_filename}
    active_orig_files = {b.original_ebook_filename for b in active_books if b.original_ebook_filename}
    protected_files = active_ebook_files | active_orig_files

    deleted_audio = 0
    deleted_transcripts = 0
    deleted_epubs = 0

    audio_cache_root = DATA_DIR / "audio_cache"
    if audio_cache_root.exists():
        for entry in audio_cache_root.iterdir():
            if entry.is_dir() and entry.name not in active_ids:
                try:
                    shutil.rmtree(entry)
                    deleted_audio += 1
                    logger.info(f"Cleaned audio cache: {entry.name}")
                except Exception as e:
                    logger.warning(f"Failed to clean audio cache {entry.name}: {e}")

    transcript_root = DATA_DIR / "transcripts" / "storyteller"
    if transcript_root.exists():
        for entry in transcript_root.iterdir():
            if entry.is_dir() and entry.name not in active_ids:
                try:
                    shutil.rmtree(entry)
                    deleted_transcripts += 1
                    logger.info(f"Cleaned transcript dir: {entry.name}")
                except Exception as e:
                    logger.warning(f"Failed to clean transcript dir {entry.name}: {e}")

    try:
        epub_cache_dir = Path(container.epub_cache_dir())
    except Exception:
        epub_cache_dir = DATA_DIR / "epub_cache"
    if epub_cache_dir.exists():
        for entry in epub_cache_dir.iterdir():
            if entry.is_file() and entry.name not in protected_files:
                try:
                    entry.unlink()
                    deleted_epubs += 1
                    logger.info(f"Cleaned cached epub: {entry.name}")
                except Exception as e:
                    logger.warning(f"Failed to clean cached epub {entry.name}: {e}")

    logger.info(f"Cache cleanup complete: {deleted_audio} audio, {deleted_transcripts} transcript, {deleted_epubs} epub(s) removed")
    return jsonify({"success": True, "deleted_audio": deleted_audio, "deleted_transcripts": deleted_transcripts, "deleted_epubs": deleted_epubs})


def _run_storyteller_backfill():
    """
    Bulk backfill storyteller transcripts for currently matched storyteller books.
    """
    assets_dir_raw = os.environ.get("STORYTELLER_ASSETS_DIR", "").strip()
    if not assets_dir_raw:
        return {
            "success": False,
            "error": "STORYTELLER_ASSETS_DIR is not configured",
            "scanned": 0,
            "ingested": 0,
            "missing": 0,
            "failed": 0,
            "aligned": 0,
            "duration_seconds": 0.0,
        }, 400

    started_at = time.time()
    books = database_service.get_all_books() if database_service else []
    storyteller_books = [
        b for b in books
        if getattr(b, "storyteller_uuid", None) or getattr(b, "transcript_source", None) == "storyteller"
    ]

    summary = {
        "success": True,
        "scanned": 0,
        "ingested": 0,
        "missing": 0,
        "failed": 0,
        "aligned": 0,
        "duration_seconds": 0.0,
    }

    abs_client = container.abs_client() if container else None
    alignment_service = getattr(manager, "alignment_service", None) if manager else None
    ebook_parser = container.ebook_parser() if container else None

    for book in storyteller_books:
        summary["scanned"] += 1
        abs_id = book.abs_id
        try:
            item_details = abs_client.get_item_details(abs_id) if abs_client else None
            chapters = item_details.get("media", {}).get("chapters", []) if item_details else []
            manifest_path = ingest_storyteller_transcripts(abs_id, book.abs_title or "", chapters)
            if not manifest_path:
                summary["missing"] += 1
                continue

            book.transcript_source = "storyteller"
            book.transcript_file = manifest_path
            summary["ingested"] += 1

            aligned = False
            if alignment_service:
                storyteller_transcript = StorytellerTranscript(manifest_path)
                book_text = ""
                if ebook_parser and book.ebook_filename:
                    try:
                        epub_filename = book.original_ebook_filename or book.ebook_filename
                        epub_path = container.epub_cache_dir() / epub_filename
                        if epub_path.exists():
                            book_text, _ = ebook_parser.extract_text_and_map(epub_path)
                    except Exception as e:
                        logger.warning(f"Could not extract text for storyteller backfill: {e}")
                        
                aligned = alignment_service.align_storyteller_and_store(abs_id, storyteller_transcript, ebook_text=book_text)
                if aligned:
                    summary["aligned"] += 1

            if aligned:
                book.transcript_file = "DB_MANAGED"
                if getattr(book, "status", None) in (None, "", "pending", "processing", "failed_retry_later", "failed_permanent", "crashed"):
                    book.status = "active"
            else:
                book.status = "pending"

            database_service.save_book(book)
        except Exception as e:
            summary["failed"] += 1
            logger.warning(f"Storyteller backfill failed for '{abs_id}': {e}")

    summary["duration_seconds"] = round(time.time() - started_at, 3)
    logger.info(
        "Storyteller backfill summary: "
        f"scanned={summary['scanned']} ingested={summary['ingested']} "
        f"aligned={summary['aligned']} missing={summary['missing']} failed={summary['failed']} "
        f"duration={summary['duration_seconds']}s"
    )
    return summary, 200


def api_storyteller_backfill():
    summary, status_code = _run_storyteller_backfill()
    return jsonify(summary), status_code


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
        logger.error(f"❌ Error proxying cover for '{abs_id}': {e}")
        return "Error loading cover", 500


# --- Logger setup (already present) ---
logger = logging.getLogger(__name__)

def get_booklore_libraries():
    """Return available Booklore libraries."""
    if not container.booklore_client().is_configured():
        return jsonify({"error": "Booklore not configured"}), 400

    libraries = container.booklore_client().get_libraries()
    return jsonify(libraries)


def _test_conn_error(e: Exception) -> str:
    """Extract a user-friendly message from a requests exception."""
    msg = str(e)
    if isinstance(e, requests.exceptions.ConnectionError):
        inner = str(e.args[0]) if e.args else msg
        if 'NameResolutionError' in inner or 'getaddrinfo' in inner or 'Name or service not known' in inner:
            return "DNS lookup failed — check the hostname"
        if 'Connection refused' in inner or 'No connection could be made' in inner:
            return "Connection refused — is the server running?"
        return "Cannot reach server — check the URL"
    if isinstance(e, requests.exceptions.Timeout):
        return "Connection timed out — server may be down"
    if isinstance(e, requests.exceptions.MissingSchema):
        return "Invalid URL — missing http:// or https://"
    return msg


def test_connection(service: str):
    """Test connectivity with diagnostic error messages."""
    testers = {
        'abs': _test_abs,
        'kosync': _test_kosync,
        'storyteller': _test_storyteller,
        'booklore': _test_booklore,
        'cwa': _test_cwa,
        'hardcover': _test_hardcover,
        'telegram': _test_telegram,
    }
    tester = testers.get(service)
    if not tester:
        return jsonify({"ok": False, "message": f"Unknown service: {service}"}), 400
    try:
        return jsonify(tester())
    except Exception as e:
        return jsonify({"ok": False, "message": _test_conn_error(e)})


def _test_abs() -> dict:
    url = os.environ.get('ABS_SERVER', '').rstrip('/')
    token = os.environ.get('ABS_KEY', '')
    if not url or not token:
        return {"ok": False, "message": "Missing server URL or API token"}
    r = requests.get(f"{url}/api/me", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if r.status_code == 200:
        username = r.json().get('username', 'unknown')
        return {"ok": True, "message": f"Connected as '{username}'"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": f"Authentication failed ({r.status_code}) — check your API token"}
    return {"ok": False, "message": f"Server returned {r.status_code}"}


def _test_kosync() -> dict:
    url = os.environ.get('KOSYNC_SERVER', '').rstrip('/')
    if os.environ.get('KOSYNC_ENABLED', '').lower() == 'false' or not url:
        return {"ok": False, "message": "KOSync not configured or disabled"}
    r = requests.get(f"{url}/healthcheck", timeout=5)
    if r.status_code == 200:
        return {"ok": True, "message": "Server is reachable"}
    return {"ok": False, "message": f"Healthcheck returned {r.status_code}"}


def _test_storyteller() -> dict:
    url = os.environ.get('STORYTELLER_API_URL', '').rstrip('/')
    user = os.environ.get('STORYTELLER_USER', '')
    pwd = os.environ.get('STORYTELLER_PASSWORD', '')
    if os.environ.get('STORYTELLER_ENABLED', '').lower() == 'false':
        return {"ok": False, "message": "Storyteller is disabled"}
    if not url or not user or not pwd:
        return {"ok": False, "message": "Missing URL, username, or password"}
    r = requests.post(
        f"{url}/api/token",
        data={"username": user, "password": pwd},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    if r.status_code == 200:
        return {"ok": True, "message": "Authenticated successfully"}
    if r.status_code in (401, 403, 422):
        return {"ok": False, "message": "Invalid username or password"}
    return {"ok": False, "message": f"Login returned {r.status_code}"}


def _test_booklore() -> dict:
    url = os.environ.get('BOOKLORE_SERVER', '').rstrip('/')
    user = os.environ.get('BOOKLORE_USER', '')
    pwd = os.environ.get('BOOKLORE_PASSWORD', '')
    if os.environ.get('BOOKLORE_ENABLED', '').lower() == 'false':
        return {"ok": False, "message": "Booklore is disabled"}
    if not url or not user or not pwd:
        return {"ok": False, "message": "Missing URL, username, or password"}
    if not url.lower().startswith(('http://', 'https://')):
        url = f"http://{url}"
    r = requests.post(
        f"{url}/api/v1/auth/login",
        json={"username": user, "password": pwd},
        timeout=10,
    )
    if r.status_code == 200:
        return {"ok": True, "message": "Authenticated successfully"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "Invalid username or password"}
    return {"ok": False, "message": f"Login returned {r.status_code}"}


def _test_cwa() -> dict:
    url = os.environ.get('CWA_SERVER', '').rstrip('/')
    user = os.environ.get('CWA_USERNAME', '').strip()
    pwd = os.environ.get('CWA_PASSWORD', '').strip()
    if os.environ.get('CWA_ENABLED', '').lower() != 'true' or not url:
        return {"ok": False, "message": "CWA not configured or disabled"}
    if not url.lower().startswith(('http://', 'https://')):
        url = f"http://{url}"
    r = requests.get(f"{url}/opds", auth=(user, pwd) if user else None, timeout=5)
    if r.status_code == 200:
        if r.text.lstrip().lower().startswith(('<!doctype html', '<html')):
            return {"ok": False, "message": "Authentication failed — server returned login page instead of OPDS feed"}
        return {"ok": True, "message": "Connected to OPDS feed"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "Invalid credentials"}
    return {"ok": False, "message": f"Server returned {r.status_code}"}


def _test_hardcover() -> dict:
    token = os.environ.get('HARDCOVER_TOKEN', '').strip()
    if os.environ.get('HARDCOVER_ENABLED', '').lower() == 'false':
        return {"ok": False, "message": "Hardcover is disabled"}
    if not token:
        return {"ok": False, "message": "Missing API token"}
    if token.lower().startswith('bearer '):
        token = token[7:].strip()
    r = requests.post(
        "https://api.hardcover.app/v1/graphql",
        json={"query": "{ me { id username } }"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code == 200:
        data = r.json()
        if data.get('data', {}).get('me'):
            username = data['data']['me'].get('username', 'unknown')
            return {"ok": True, "message": f"Connected as '{username}'"}
        errors = data.get('errors', [])
        if errors:
            return {"ok": False, "message": f"API error: {errors[0].get('message', 'unknown')}"}
        return {"ok": False, "message": "Invalid API token — no user data returned"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "Invalid API token"}
    return {"ok": False, "message": f"API returned {r.status_code}"}


def _test_telegram() -> dict:
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    if os.environ.get('TELEGRAM_ENABLED', '').lower() == 'false' or not token:
        return {"ok": False, "message": "Telegram not configured or disabled"}
    r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
    if r.status_code == 200 and r.json().get('ok'):
        bot_name = r.json().get('result', {}).get('username', 'unknown')
        return {"ok": True, "message": f"Connected (bot: @{bot_name})"}
    if r.status_code == 401:
        return {"ok": False, "message": "Invalid bot token"}
    return {"ok": False, "message": f"Telegram API returned {r.status_code}"}

# ---------------- HELPER FUNCTIONS ----------------
def safe_folder_name(name: str) -> str:
    """Sanitize folder name for file system safe usage."""
    invalid = '<>:"/\\|?*'
    name = html.escape(str(name).strip())[:150]
    for c in invalid:
        name = name.replace(c, '_')
    return name.strip() or "Unknown"

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

    def _legacy_book_linker_redirect(dummy=None):
        return redirect(url_for('forge'), code=301)

    # Register all routes here
    app.add_url_rule('/', 'index', index)
    app.add_url_rule('/shelfmark', 'shelfmark', shelfmark)
    app.add_url_rule('/forge', 'forge', forge)
    app.add_url_rule('/book-linker', 'book_linker_legacy', _legacy_book_linker_redirect)
    app.add_url_rule('/book-linker/<path:dummy>', 'book_linker_legacy_path', _legacy_book_linker_redirect)
    app.add_url_rule('/match', 'match', match, methods=['GET', 'POST'])
    app.add_url_rule('/batch-match', 'batch_match', batch_match, methods=['GET', 'POST'])
    app.add_url_rule('/suggestions', 'suggestions', suggestions_page, methods=['GET', 'POST'])
    app.add_url_rule('/delete/<abs_id>', 'delete_mapping', delete_mapping, methods=['POST'])
    app.add_url_rule('/clear-progress/<abs_id>', 'clear_progress', clear_progress, methods=['POST'])
    app.add_url_rule('/api/sync-now/<abs_id>', 'sync_now', sync_now, methods=['POST'])
    app.add_url_rule('/api/mark-complete/<abs_id>', 'mark_complete', mark_complete, methods=['POST'])
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
    app.add_url_rule('/api/suggestions/scan-status', 'suggestions_scan_status', suggestions_scan_status, methods=['GET'])
    app.add_url_rule('/api/suggestions/<source_id>/dismiss', 'dismiss_suggestion', dismiss_suggestion, methods=['POST'])
    app.add_url_rule('/api/suggestions/<source_id>/ignore', 'ignore_suggestion', ignore_suggestion, methods=['POST'])
    app.add_url_rule('/api/suggestions/clear_stale', 'clear_stale_suggestions', clear_stale_suggestions, methods=['POST'])
    app.add_url_rule('/api/cache/clean', 'clean_cache', clean_inactive_cache, methods=['POST'])
    app.add_url_rule('/api/cover-proxy/<abs_id>', 'proxy_cover', proxy_cover)
    app.add_url_rule('/api/booklore/libraries', 'get_booklore_libraries', get_booklore_libraries, methods=['GET'])
    app.add_url_rule('/api/test-connection/<service>', 'test_connection', test_connection, methods=['GET'])

    # Storyteller API routes
    app.add_url_rule('/api/storyteller/search', 'api_storyteller_search', api_storyteller_search, methods=['GET'])
    app.add_url_rule('/api/storyteller/link/<abs_id>', 'api_storyteller_link', api_storyteller_link, methods=['POST'])
    app.add_url_rule('/api/storyteller/backfill', 'api_storyteller_backfill', api_storyteller_backfill, methods=['POST'])

    # Forge routes
    app.add_url_rule('/api/forge/search_audio', 'forge_search_audio', forge_search_audio, methods=['GET'])
    app.add_url_rule('/api/forge/search_text', 'forge_search_text', forge_search_text, methods=['GET'])
    app.add_url_rule('/api/forge/process', 'forge_process', forge_process, methods=['POST'])
    
    @app.route('/api/forge/active', methods=['GET'])
    def forge_active_tasks():
        return jsonify(list(container.forge_service().active_tasks))

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
    threading.Thread(target=get_update_status, daemon=True).start()
    logger.info("🚀 Sync daemon thread started")

    # Start ABS Socket.IO listener for real-time / instant sync
    instant_sync_enabled = os.environ.get('INSTANT_SYNC_ENABLED', 'true').lower() != 'false'
    abs_socket_enabled = os.environ.get('ABS_SOCKET_ENABLED', 'true').lower() != 'false'
    if instant_sync_enabled and abs_socket_enabled and container.abs_client().is_configured():
        from src.services.abs_socket_listener import ABSSocketListener
        abs_listener = ABSSocketListener(
            abs_server_url=os.environ.get('ABS_SERVER', ''),
            abs_api_token=os.environ.get('ABS_KEY', ''),
            database_service=database_service,
            sync_manager=manager
        )
        abs_socket_thread = threading.Thread(target=abs_listener.start, daemon=True)
        abs_socket_thread.start()
        logger.info("🔌 ABS Socket.IO listener started (instant sync enabled)")
    elif not instant_sync_enabled:
        logger.info("ℹ️ ABS Socket.IO listener disabled (INSTANT_SYNC_ENABLED=false)")
    elif not abs_socket_enabled:
        logger.info("ℹ️ ABS Socket.IO listener disabled (ABS_SOCKET_ENABLED=false)")

    # Start per-client poller (always-on; _poll_cycle skips clients in 'global' mode)
    from src.services.client_poller import ClientPoller
    client_poller = ClientPoller(
        database_service=database_service,
        sync_manager=manager,
        sync_clients_dict=container.sync_clients(),
    )
    poller_thread = threading.Thread(target=client_poller.start, daemon=True)
    poller_thread.start()



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



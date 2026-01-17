# [START FILE: abs-kosync-enhanced/web_server.py]
import html
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
import schedule
from flask import Flask, render_template, request, redirect, url_for, jsonify, session

from src.db.database_service import DatabaseService
from src.sync_manager import SyncManager
from src.utils.di_container import Container
# Import memory logging setup - this must happen early to capture all logs
from src.utils.logging_utils import memory_log_handler, LOG_PATH
from src.utils.logging_utils import sanitize_log_data
from src.utils.di_container import create_container, Container
from src.db.migration_utils import initialize_database
from src.utils.config_loader import ConfigLoader
from dependency_injector import providers

# ---------------- APP SETUP ----------------

# Get static and template folder paths from environment variables, with fallback
STATIC_DIR = os.environ.get('STATIC_DIR', '/app/static')
TEMPLATE_DIR = os.environ.get('TEMPLATE_DIR', '/app/templates')

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='/static', template_folder=TEMPLATE_DIR)
app.secret_key = "kosync-queue-secret-unified-app"

# NOTE: Logging is configured centrally in `main.py`. Avoid calling
# `logging.basicConfig` here to prevent adding duplicate handlers.
logger = logging.getLogger(__name__)

# Global variables - will be initialized via setup_dependencies()
container: Optional[Container] = None
manager: Optional[SyncManager] = None
database_service: Optional[DatabaseService] = None
DATA_DIR: Optional[Path] = None
EBOOK_DIR: Optional[Path] = None

def setup_dependencies(test_container=None):
    """
    Initialize dependencies for the web server.

    Args:
        test_container: Optional test container for dependency injection during testing.
                       If None, creates production container from environment.
    """
    global container, manager, database_service, DATA_DIR, EBOOK_DIR

    # 1. Initialize Database Service FIRST (needed to load settings)
    # We use a temporary DATA_DIR for this initial connection, usually defaults to /data
    # If the user changed DATA_DIR in settings, we might need a two-stage init, but 
    # for now we assume the DB location itself doesn't move dynamically based on DB settings 
    # (chicken and egg).
    initial_data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    from src.db.migration_utils import initialize_database
    database_service = initialize_database(initial_data_dir)

    # 2. LOAD SETTINGS FROM DB (Priority Level 1)
    # This updates os.environ with values from the database
    if database_service:
        ConfigLoader.load_settings(database_service)
        logger.info("✅ Settings loaded into environment variables")

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
    container.database_service.override(providers.Object(database_service))

    # Initialize manager and services
    manager = container.sync_manager()

    # Get data directories (now using updated env vars)
    DATA_DIR = container.data_dir()
    EBOOK_DIR = container.books_dir()

    logger.info(f"Web server dependencies initialized (DATA_DIR={DATA_DIR})")

# Book Linker - source ebooks for Storyteller workflow
LINKER_BOOKS_DIR = Path(os.environ.get("LINKER_BOOKS_DIR", "/linker_books"))

# Book Linker - Storyteller processing folder
DEST_BASE = Path(os.environ.get("PROCESSING_DIR", "/processing"))

# Book Linker - Storyteller final ingest folder
STORYTELLER_INGEST = Path(os.environ.get("STORYTELLER_INGEST_DIR", os.environ.get("LINKER_BOOKS_DIR", "/linker_books")))

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

MONITOR_INTERVAL = int(os.environ.get("MONITOR_INTERVAL", "3600"))  # Default 1 hour

# ---------------- BOOK LINKER HELPERS ----------------

def safe_folder_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    name = html.escape(str(name).strip())[:150]
    for c in invalid:
        name = name.replace(c, '_')
    return name.strip() or "Unknown"


app.jinja_env.globals['safe_folder_name'] = safe_folder_name

def get_stats(ebooks, audiobooks):
    total = sum(m["file_size_mb"] for m in ebooks) + sum(m.get("file_size_mb", 0) for m in audiobooks)
    return {
        "ebook_count": len(ebooks),
        "audio_count": len(audiobooks),
        "total_count": len(ebooks) + len(audiobooks),
        "total_size_mb": round(total, 2),
    }


def search_abs_audiobooks_linker(query: str):
    """Search ABS for audiobooks - Book Linker version"""
    try:
        logger.info(f"🔍 Book Linker searching for: '{query}'")

        # Get all audiobooks
        all_audiobooks = container.abs_client().get_all_audiobooks()
        logger.info(f"📚 Got {len(all_audiobooks)} total audiobooks from ABS")

        if not query:
            logger.warning("⚠️ Empty query provided")
            return []

        query_lower = query.lower()
        results = []

        for ab in all_audiobooks:
            # Use the SAME matching logic as Single/Batch
            if audiobook_matches_search(ab, query_lower):
                # Get full item details to access audio files
                item_details = container.abs_client().get_item_details(ab.get('id'))
                if not item_details:
                    continue

                media = item_details.get('media', {})
                metadata = media.get('metadata', {})
                audio_files = media.get('audioFiles', [])

                title = metadata.get('title', ab.get('name', 'Unknown'))
                logger.debug(f"  ✅ Matched: {title}")

                if not audio_files:
                    logger.debug(f"  ⚠️ Skipping {title} - no audio files")
                    continue

                size_mb = sum(f.get('metadata', {}).get('size', 0) for f in audio_files) / (1024 * 1024)

                results.append({
                    "id": ab.get("id"),
                    "title": title,
                    "author": metadata.get('authorName') or get_abs_author(ab),
                    "file_size_mb": round(size_mb, 2),
                    "num_files": len(audio_files),
                })

        logger.info(f"📊 Found {len(results)} matching audiobooks")
        return results

    except Exception as e:
        logger.error(f"❌ Book Linker ABS search failed: {e}", exc_info=True)
        return []


def copy_abs_audiobook_linker(abs_id: str, dest_folder: Path):
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


def find_local_ebooks(query: str):
    """Find ebooks in Book Linker source folder"""
    matches = []
    query_lower = query.lower()
    if not LINKER_BOOKS_DIR.exists(): return matches

    for epub in LINKER_BOOKS_DIR.rglob("*.epub"):
        if "(readaloud)" in epub.name.lower(): continue
        if query_lower in epub.name.lower():
            matches.append({
                "full_path": str(epub),
                "file_name": epub.name,
                "file_size_mb": round(epub.stat().st_size / (1024 * 1024), 2),
            })
    return matches


# ---------------- MONITORING LOGIC (RESTORED) ----------------

def run_processing_scan(manual=False):
    """
    Shared logic to scan the processing folder.
    Used by both the background thread and the 'Check Now' button.
    """
    processed = 0
    skipped = 0
    MIN_AGE_MINUTES = 10

    try:
        if not DEST_BASE.exists():
            if manual: logger.warning(f"Destination base does not exist: {DEST_BASE}")
            return 0, 0

        for folder in DEST_BASE.iterdir():
            if not folder.is_dir(): continue

            readaloud_files = list(folder.glob("*readaloud*.epub"))
            if not readaloud_files: continue

            for readaloud_file in readaloud_files:
                try:
                    # 1. Age Check
                    file_mtime = readaloud_file.stat().st_mtime
                    file_age_minutes = (time.time() - file_mtime) / 60

                    if file_age_minutes < MIN_AGE_MINUTES:
                        logger.info(f"Skipping {readaloud_file.name} - too recent ({file_age_minutes:.1f} min)")
                        skipped += 1
                        continue

                    # 2. Process Lock Check
                    folder_name = folder.name
                    storyteller_active = False
                    try:
                        result = subprocess.run(['lsof', '+D', str(folder)], capture_output=True, text=True, timeout=5)
                        if result.stdout.strip(): storyteller_active = True
                    except:
                        try:
                            ps_result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
                            for line in ps_result.stdout.split('\n'):
                                if folder_name in line and ('node' in line.lower() or 'storyteller' in line.lower()):
                                    storyteller_active = True
                                    break
                        except:
                            pass

                    if storyteller_active:
                        skipped += 1
                        continue

                    # 3. Modification Check
                    all_files = list(folder.rglob("*"))
                    if all_files:
                        file_times = [f.stat().st_mtime for f in all_files if f.is_file()]
                        if file_times:
                            newest_file_time = max(file_times)
                            folder_age_minutes = (time.time() - newest_file_time) / 60
                            if folder_age_minutes < MIN_AGE_MINUTES:
                                skipped += 1
                                continue

                    # 4. Clean up and Move
                    all_files_in_folder = list(folder.iterdir())
                    deleted_count = 0
                    for file in all_files_in_folder:
                        if not file.is_file(): continue
                        if file == readaloud_file: continue
                        try:
                            file.unlink()
                            deleted_count += 1
                        except:
                            pass

                    ingest_dest = STORYTELLER_INGEST / folder.name
                    if ingest_dest.exists(): shutil.rmtree(str(ingest_dest))

                    shutil.move(str(folder), str(ingest_dest))
                    logger.info(f"Processed: {ingest_dest} (Deleted {deleted_count} sources)")
                    processed += 1

                except Exception as e:
                    logger.error(f"Error processing {readaloud_file}: {e}")
                    skipped += 1

    except Exception as e:
        logger.error(f"Scan error: {e}", exc_info=True)

    return processed, skipped


def monitor_readaloud_files():
    while True:
        try:
            time.sleep(MONITOR_INTERVAL)
            run_processing_scan(manual=False)
        except Exception as e:
            logger.error(f"Monitor loop error: {e}", exc_info=True)


# ---------------- SYNC MANAGER DAEMON ----------------

def sync_daemon():
    """Background sync daemon running in a separate thread."""
    try:
        # Setup schedule for sync operations
        sync_period = int(os.environ.get("SYNC_PERIOD_MINS", "5"))
        schedule.every(sync_period).minutes.do(manager.sync_cycle)
        schedule.every(1).minutes.do(manager.check_pending_jobs)

        logger.info(f"🔄 Sync daemon started (period: {sync_period} minutes)")

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
    matches = list(base.rglob(filename))
    return matches[0] if matches else None


def get_kosync_id_for_ebook(ebook_filename, booklore_id=None):
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
    if ebook_path:
        return container.ebook_parser().get_kosync_id(ebook_path)

    # Neither source available - log helpful warning
    if not container.booklore_client().is_configured() and not EBOOK_DIR.exists():
        logger.warning(
            f"Cannot compute KOSync ID for '{ebook_filename}': "
            "Neither Booklore integration nor /books volume is configured. "
            "Enable Booklore (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD) "
            "or mount the ebooks directory to /books."
        )
    elif not booklore_id and not ebook_path:
        logger.warning(f"Cannot compute KOSync ID for '{ebook_filename}': File not found in Booklore or filesystem")

    return None


class EbookResult:
    """Wrapper to provide consistent interface for ebooks from Booklore or filesystem."""

    def __init__(self, name, title=None, subtitle=None, authors=None, booklore_id=None, path=None):
        self.name = name
        self.title = title or Path(name).stem
        self.subtitle = subtitle or ''
        self.authors = authors or ''
        self.booklore_id = booklore_id
        self._path = path
        self.has_metadata = booklore_id is not None

    @property
    def display_name(self):
        """Format: 'Author - Title: Subtitle' for Booklore, filename for filesystem."""
        if self.has_metadata and self.authors:
            full_title = self.title
            if self.subtitle:
                full_title = f"{self.title}: {self.subtitle}"
            return f"{self.authors} - {full_title}"
        return self.name

    @property
    def stem(self):
        return Path(self.name).stem

    def __str__(self):
        return self.name


def get_searchable_ebooks(search_term):
    """Get ebooks from Booklore API if available, otherwise filesystem.
    Returns list of EbookResult objects for consistent interface."""

    # Try Booklore first if configured
    if container.booklore_client().is_configured():
        try:
            books = container.booklore_client().search_books(search_term)
            if books:
                return [
                    EbookResult(
                        name=b.get('fileName', ''),
                        title=b.get('title'),
                        subtitle=b.get('subtitle'),
                        authors=b.get('authors'),
                        booklore_id=b.get('id')
                    )
                    for b in books if b.get('fileName', '').lower().endswith('.epub')
                ]
        except Exception as e:
            logger.warning(f"Booklore search failed, falling back to filesystem: {e}")

    # Fallback to filesystem
    if not EBOOK_DIR.exists():
        if not container.booklore_client().is_configured():
            logger.warning(
                "No ebooks available: Neither Booklore integration nor /books volume is configured. "
                "Enable Booklore (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD) "
                "or mount the ebooks directory to /books."
            )
        return []

    all_epubs = list(EBOOK_DIR.glob("**/*.epub"))
    if not search_term:
        return [EbookResult(name=eb.name, path=eb) for eb in all_epubs]

    return [
        EbookResult(name=eb.name, path=eb)
        for eb in all_epubs
        if search_term.lower() in eb.name.lower()
    ]



def restart_server():
    """Restarts the current Python process to reload settings."""
    logger.info("♻️  Restarting application to apply new settings...")
    time.sleep(1.5)  # Give Flask time to send the redirect response
    # Re-execute the current script with the same arguments
    # This replaces the current process, so the PID stays the same
    os.execv(sys.executable, [sys.executable] + sys.argv)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    # Priority Level 3: Application Defaults
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
        'TELEGRAM_LOG_LEVEL': 'ERROR'
    }

    if request.method == 'POST':
        bool_keys = [
            'KOSYNC_USE_PERCENTAGE_FROM_SERVER', 
            'SYNC_ABS_EBOOK', 
            'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT'
        ]
        
        # Current settings in DB
        current_settings = database_service.get_all_settings()
        
        # 1. Handle Boolean Toggles (Checkbox logic)
        # Checkboxes are NOT sent if unchecked, so we must check every known bool key
        for key in bool_keys:
            is_checked = (key in request.form)
            # Save "true" or "false"
            database_service.set_setting(key, str(is_checked).lower())

        # 2. Handle Text Inputs
        # Iterate over form to find other keys
        for key, value in request.form.items():
            if key in bool_keys: continue
            
            clean_value = value.strip()
            
            # Special handling: If empty, deciding whether to delete or save empty
            # Strategy: If it was previously set, allow clearing it? 
            # Or just save empty string? 
            # User snippet logic: Only save non-empty. Let's stick to that for now, 
            # BUT if it's in DB and now empty, we probably want to update it to empty/delete?
            # Let's save standard string representation.
            
            if clean_value:
                database_service.set_setting(key, clean_value)
            elif key in current_settings:
                # If key exists in DB but user cleared it, set to empty (or delete?)
                # Setting to empty string is safer than deleting if defaults exist
                database_service.set_setting(key, "")

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
    
    # helper to get value from Env (which is loaded from DB) > Defaults
    def get_val(key, default_val=None):
        if key in os.environ: return os.environ[key]
        if key in DEFAULTS: return DEFAULTS[key]
        return default_val if default_val is not None else ''
        
    def get_bool(key):
        val = os.environ.get(key, 'false')
        return val.lower() in ('true', '1', 'yes', 'on')

    return render_template('settings.html', 
                         get_val=get_val, 
                         get_bool=get_bool, 
                         message=message, 
                         is_error=is_error)

def get_abs_author(ab):

    """Extract author from ABS audiobook metadata."""
    media = ab.get('media', {})
    metadata = media.get('metadata', {})
    return metadata.get('authorName') or (metadata.get('authors') or [{}])[0].get("name", "")


def audiobook_matches_search(ab, search_term):
    """Check if audiobook matches search term (searches title AND author)."""
    title = manager.get_abs_title(ab).lower()
    author = get_abs_author(ab).lower()
    return search_term in title or search_term in author

# ---------------- ROUTES ----------------
@app.route('/')
def index():
    """Dashboard - loads books and progress from database service"""

    # Load books from database service
    books = database_service.get_all_books()

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
        # Get states for this book from database service
        states = database_service.get_states_for_book(book.abs_id)

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
            'unified_progress': 0,
            'duration': book.duration or 0,
            'states': {}
        }

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
        hardcover_details = database_service.get_hardcover_details(book.abs_id)
        if hardcover_details:
            mapping.update({
                'hardcover_book_id': hardcover_details.hardcover_book_id,
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
                'hardcover_edition_id': None,
                'hardcover_pages': None,
                'isbn': None,
                'asin': None,
                'matched_by': None,
                'hardcover_linked': False,
                'hardcover_title': None
            })

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

    return render_template('index.html', mappings=mappings, integrations=integrations, progress=overall_progress)


@app.route('/book-linker', methods=['GET', 'POST'])
def book_linker():
    message = session.pop("message", None)
    is_error = session.pop("is_error", False)
    book_name = ""
    ebook_matches = []
    audiobook_matches = []
    stats = None

    if request.method == "POST":
        book_name = request.form["book_name"].strip()
        if book_name:
            ebook_matches = find_local_ebooks(book_name)
            audiobook_matches = search_abs_audiobooks_linker(book_name)
            stats = get_stats(ebook_matches, audiobook_matches)

    return render_template('book_linker.html', book_name=book_name, ebook_matches=ebook_matches, audiobook_matches=audiobook_matches, stats=stats,
                           message=message, is_error=is_error, linker_books_dir=str(LINKER_BOOKS_DIR), processing_dir=str(DEST_BASE),
                           storyteller_ingest=str(STORYTELLER_INGEST))


@app.route('/book-linker/process', methods=['POST'])
def book_linker_process():
    book_name = request.form.get("book_name", "").strip()
    if not book_name:
        session["message"] = "Error: No book name"
        session["is_error"] = True
        return redirect(url_for('book_linker'))

    selected_ebooks = request.form.getlist("ebook")
    folder_name = book_name
    if selected_ebooks: folder_name = Path(selected_ebooks[0]).stem

    safe_name = safe_folder_name(folder_name)
    dest = DEST_BASE / safe_name
    dest.mkdir(parents=True, exist_ok=True)
    count = 0

    for path in selected_ebooks:
        src = Path(path)
        if src.exists():
            shutil.copy2(str(src), dest / src.name)
            count += 1

    for abs_id in request.form.getlist("audiobook"):
        if copy_abs_audiobook_linker(abs_id, dest): count += 1

    session["message"] = f"Success: {count} items -> {safe_name}"
    session["is_error"] = False
    return redirect(url_for('book_linker'))


@app.route('/book-linker/trigger-monitor', methods=['POST'])
def trigger_monitor():
    processed, skipped = run_processing_scan(manual=True)
    if processed > 0:
        session["message"] = f"Manual scan complete: Processed {processed} items."
        session["is_error"] = False
    elif skipped > 0:
        session["message"] = f"Manual scan complete: Skipped {skipped} items (too new or in use)."
        session["is_error"] = False
    else:
        session["message"] = "Manual scan complete: No ready items found."
        session["is_error"] = False
    return redirect(url_for('book_linker'))


@app.route('/match', methods=['GET', 'POST'])
def match():
    if request.method == 'POST':
        abs_id = request.form.get('audiobook_id')
        ebook_filename = request.form.get('ebook_filename')
        audiobooks = container.abs_client().get_all_audiobooks()
        selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
        if not selected_ab: return "Audiobook not found", 404

        # Get booklore_id if available for API-based hash computation
        booklore_id = None
        if container.booklore_client().is_configured():
            book = container.booklore_client().find_book_by_filename(ebook_filename)
            if book:
                booklore_id = book.get('id')

        # Compute KOSync ID (Booklore API first, filesystem fallback)
        kosync_doc_id = get_kosync_id_for_ebook(ebook_filename, booklore_id)
        if not kosync_doc_id:
            logger.warning(f"Cannot compute KOSync ID for '{sanitize_log_data(ebook_filename)}': File not found in Booklore or filesystem")
            return "Could not compute KOSync ID for ebook", 404

        # Create Book object and save to database service
        from src.db.models import Book
        book = Book(
            abs_id=abs_id,
            abs_title=manager.get_abs_title(selected_ab),
            ebook_filename=ebook_filename,
            kosync_doc_id=kosync_doc_id,
            transcript_file=None,
            status="pending",
            duration=manager.get_duration(selected_ab)
        )

        database_service.save_book(book)
        container.abs_client().add_to_collection(abs_id)
        container.booklore_client().add_to_shelf(ebook_filename)
        container.storyteller_client().add_to_collection(ebook_filename)
        return redirect(url_for('index'))

    search = request.args.get('search', '').strip().lower()
    audiobooks, ebooks = [], []
    if search:
        audiobooks = container.abs_client().get_all_audiobooks()
        audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
        for ab in audiobooks: ab['cover_url'] = f"{container.abs_client().base_url}/api/items/{ab['id']}/cover?token={container.abs_client().token}"

        # Use new search method
        ebooks = get_searchable_ebooks(search)

    return render_template('match.html', audiobooks=audiobooks, ebooks=ebooks, search=search, get_title=manager.get_abs_title)


@app.route('/batch-match', methods=['GET', 'POST'])
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
                    duration=duration
                )

                database_service.save_book(book)
                container.abs_client().add_to_collection(item['abs_id'])
                container.booklore_client().add_to_shelf(item['ebook_filename'])
                container.storyteller_client().add_to_collection(item['ebook_filename'])

            session['queue'] = []
            session.modified = True
            return redirect(url_for('index'))

    search = request.args.get('search', '').strip().lower()
    audiobooks, ebooks = [], []
    if search:
        audiobooks = container.abs_client().get_all_audiobooks()
        audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
        for ab in audiobooks: ab['cover_url'] = f"{container.abs_client().base_url}/api/items/{ab['id']}/cover?token={container.abs_client().token}"

        # Use new search method
        ebooks = get_searchable_ebooks(search)
        ebooks.sort(key=lambda x: x.name.lower())

    return render_template('batch_match.html', audiobooks=audiobooks, ebooks=ebooks, queue=session.get('queue', []), search=search,
                           get_title=manager.get_abs_title)


@app.route('/delete/<abs_id>', methods=['POST'])
def delete_mapping(abs_id):
    # Get book from database service
    book = database_service.get_book(abs_id)
    if book:
        # Clean up transcript file if it exists
        if book.transcript_file:
            try:
                Path(book.transcript_file).unlink()
            except:
                pass

    # Delete book and all associated data (states, jobs, hardcover details) via database service
    database_service.delete_book(abs_id)

    return redirect(url_for('index'))


@app.route('/clear-progress/<abs_id>', methods=['POST'])
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


@app.route('/link-hardcover/<abs_id>', methods=['POST'])
def link_hardcover(abs_id):
    from flask import flash
    url = request.form.get('hardcover_url', '').strip()
    if not url:
        return redirect(url_for('index'))

    # Resolve book
    book_data = manager.hardcover_client.resolve_book_from_input(url)
    if not book_data:
        flash(f"❌ Could not find book for: {url}", "error")
        return redirect(url_for('index'))

    # Create or update hardcover details using database service
    from src.db.models import HardcoverDetails

    try:
        hardcover_details = HardcoverDetails(
            abs_id=abs_id,
            hardcover_book_id=book_data['book_id'],
            hardcover_edition_id=book_data.get('edition_id'),
            hardcover_pages=book_data.get('pages'),
            matched_by='manual'  # Since this was manually linked
        )

        database_service.save_hardcover_details(hardcover_details)

        # Force status to 'Want to Read' (1)
        try:
            manager.hardcover_client.update_status(book_data['book_id'], 1, book_data.get('edition_id'))
        except Exception as e:
            logger.warning(f"Failed to set Hardcover status: {e}")

        flash(f"✅ Linked Hardcover: {book_data.get('title')}", "success")
    except Exception as e:
        logger.error(f"Failed to save hardcover details: {e}")
        flash("❌ Database update failed", "error")

    return redirect(url_for('index'))


@app.route('/api/status')
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
            'duration': book.duration,
            'states': {}
        }

        # Add progress information from states
        for client_name, state in state_by_client.items():
            # Store in unified states object
            mapping['states'][client_name] = {
                'timestamp': state.timestamp or 0,
                'percentage': state.percentage or 0,
                'xpath': getattr(state, 'xpath', None),
                'last_updated': state.last_updated
            }

            # Maintain backward compatibility with old field names
            if client_name == 'kosync':
                mapping['kosync_pct'] = state.percentage
                mapping['kosync_xpath'] = getattr(state, 'xpath', None)
            elif client_name == 'abs':
                mapping['abs_pct'] = state.percentage
                mapping['abs_ts'] = state.timestamp
            elif client_name == 'storyteller':
                mapping['storyteller_pct'] = state.percentage
                mapping['storyteller_xpath'] = getattr(state, 'xpath', None)
            elif client_name == 'booklore':
                mapping['booklore_pct'] = state.percentage
                mapping['booklore_xpath'] = getattr(state, 'xpath', None)

        mappings.append(mapping)

    return jsonify({"mappings": mappings})


@app.route('/logs')
def logs_view():
    """Display logs frontend with filtering capabilities."""
    return render_template('logs.html')


@app.route('/api/logs')
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
        if LOG_PATH.exists():
            with open(LOG_PATH, 'r', encoding='utf-8') as f:
                all_lines.extend(f.readlines())

        # Read backup files if needed (for more history)
        if lines_count > len(all_lines):
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


@app.route('/api/logs/live')
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


@app.route('/view_log')
def view_log():
    """Legacy endpoint - redirect to new logs page."""
    return redirect(url_for('logs_view'))

if __name__ == '__main__':
    # Initialize dependencies for production mode
    setup_dependencies()

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

    logger.info("=== Unified ABS Manager Started (Integrated Mode) ===")

    # Start sync daemon in background thread
    sync_daemon_thread = threading.Thread(target=sync_daemon, daemon=True)
    sync_daemon_thread.start()
    logger.info("Sync daemon thread started")

    monitor_thread = threading.Thread(target=monitor_readaloud_files, daemon=True)
    monitor_thread.start()
    logger.info("Readaloud monitor started")

    # Check ebook source configuration
    booklore_configured = container.booklore_client().is_configured()
    books_volume_exists = EBOOK_DIR.exists()

    if booklore_configured:
        logger.info(f"✅ Booklore integration enabled - ebooks sourced from API")
    elif books_volume_exists:
        logger.info(f"✅ Ebooks directory mounted at {EBOOK_DIR}")
    else:
        logger.warning(
            "⚠️  NO EBOOK SOURCE CONFIGURED: Neither Booklore integration nor /books volume is available. "
            "New book matches will fail. Enable Booklore (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD) "
            "or mount the ebooks directory to /books."
        )

    logger.info(f"📁 Book Linker monitoring interval: {MONITOR_INTERVAL} seconds")
    logger.info(f"🌐 Web interface starting on port 5757")

    app.run(host='0.0.0.0', port=5757, debug=False)
# [END FILE]

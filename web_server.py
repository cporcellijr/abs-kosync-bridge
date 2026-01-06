# [START FILE: abs-kosync-enhanced/web_server.py]
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash
import logging
from pathlib import Path
from main import SyncManager
import time
import requests
import os
import shutil
import subprocess
import threading
from logging.handlers import RotatingFileHandler
from urllib.parse import urljoin
import html
from json_db import JsonDB
from logging_utils import sanitize_log_data

# ---------------- APP SETUP ----------------

app = Flask(__name__, static_folder='/app/static', static_url_path='/static')
app.secret_key = "kosync-queue-secret-unified-app"

# NOTE: Logging is configured centrally in `main.py`. Avoid calling
# `logging.basicConfig` here to prevent adding duplicate handlers.
logger = logging.getLogger(__name__)

manager = SyncManager()

db_handler = JsonDB("/data/mapping_db.json")
state_handler = JsonDB("/data/last_state.json")

# ---------------- BOOK LINKER CONFIG ----------------

# Book Matching - ebooks for sync matching (original functionality)
EBOOK_DIR = Path(os.environ.get("BOOKS_DIR", "/books"))

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

LOG_DIR = Path("/data/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "unified_app.log"

def setup_file_logging():
    file_handler = RotatingFileHandler(str(LOG_PATH), maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
    # Attach to the root logger so all module loggers go to the same file
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)
    # Prevent Werkzeug from propagating its logs up to the root logger (avoids duplicate access lines)
    logging.getLogger('werkzeug').propagate = False

setup_file_logging()

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
    headers = {"Authorization": f"Bearer {ABS_API_TOKEN}"}
    url = urljoin(ABS_API_URL, f"/api/libraries/{ABS_LIBRARY_ID}/search")
    try:
        r = requests.get(url, headers=headers, params={"q": query}, timeout=15)
        r.raise_for_status()
        results = []
        for entry in r.json().get("book", []):
            item = entry.get("libraryItem", {})
            media = item.get("media", {})
            audio_files = media.get("audioFiles", [])
            if not audio_files: continue
            size_mb = sum(f.get("metadata", {}).get("size", 0) for f in audio_files) / (1024*1024)
            meta = media.get("metadata", {})
            author = meta.get("authorName") or (meta.get("authors") or [{}])[0].get("name") or "Unknown"
            results.append({
                "id": item.get("id"),
                "title": meta.get("title", "Unknown"),
                "author": author,
                "file_size_mb": round(size_mb, 2),
                "num_files": len(audio_files),
            })
        return results
    except Exception as e:
        logger.error(f"ABS search failed: {e}")
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
                "file_size_mb": round(epub.stat().st_size / (1024*1024), 2),
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
                        except: pass
                    
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
                        except: pass
                    
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

monitor_thread = threading.Thread(target=monitor_readaloud_files, daemon=True)
monitor_thread.start()
logger.info("Readaloud monitor started")

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
    if booklore_id and manager.booklore_client.is_configured():
        try:
            content = manager.booklore_client.download_book(booklore_id)
            if content:
                kosync_id = manager.ebook_parser.get_kosync_id_from_bytes(ebook_filename, content)
                if kosync_id:
                    logger.debug(f"Computed KOSync ID from Booklore download: {kosync_id}")
                    return kosync_id
        except Exception as e:
            logger.warning(f"Failed to get KOSync ID from Booklore, falling back to filesystem: {e}")

    # Fall back to filesystem
    ebook_path = find_ebook_file(ebook_filename)
    if ebook_path:
        return manager.ebook_parser.get_kosync_id(ebook_path)

    # Neither source available - log helpful warning
    if not manager.booklore_client.is_configured() and not EBOOK_DIR.exists():
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
    if manager.booklore_client.is_configured():
        try:
            books = manager.booklore_client.search_books(search_term)
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
        if not manager.booklore_client.is_configured():
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

def get_abs_author(ab):
    """Extract author from ABS audiobook metadata."""
    media = ab.get('media', {})
    metadata = media.get('metadata', {})
    return metadata.get('authorName') or (metadata.get('authors') or [{}])[0].get("name", "")

def audiobook_matches_search(ab, search_term):
    """Check if audiobook matches search term (searches title AND author)."""
    title = manager._get_abs_title(ab).lower()
    author = get_abs_author(ab).lower()
    return search_term in title or search_term in author

def add_to_abs_collection(abs_client, item_id, collection_name=None):
    if collection_name is None: collection_name = ABS_COLLECTION_NAME
    try:
        collections_url = f"{abs_client.base_url}/api/collections"
        r = requests.get(collections_url, headers=abs_client.headers)
        if r.status_code != 200: return False
        
        collections = r.json().get('collections', [])
        target_collection = next((c for c in collections if c.get('name') == collection_name), None)
        
        if not target_collection:
            lib_url = f"{abs_client.base_url}/api/libraries"
            r_lib = requests.get(lib_url, headers=abs_client.headers)
            if r_lib.status_code == 200:
                libraries = r_lib.json().get('libraries', [])
                if libraries:
                    r_create = requests.post(collections_url, headers=abs_client.headers, json={"libraryId": libraries[0]['id'], "name": collection_name})
                    if r_create.status_code in [200, 201]: target_collection = r_create.json()
        
        if not target_collection: return False
        add_url = f"{abs_client.base_url}/api/collections/{target_collection['id']}/book"
        r_add = requests.post(add_url, headers=abs_client.headers, json={"id": item_id})
        if r_add.status_code in [200, 201, 204]:
            try:
                details = abs_client.get_item_details(item_id)
                title = details.get('media', {}).get('metadata', {}).get('title') if details else None
            except Exception:
                title = None
            logger.info(f"🏷️ Added '{sanitize_log_data(title or str(item_id))}' to ABS Collection: {collection_name}")
            return True
        return False
    except: return False

def add_to_booklore_shelf(ebook_filename, shelf_name=None):
    if shelf_name is None: shelf_name = BOOKLORE_SHELF_NAME
    booklore_url = os.environ.get("BOOKLORE_SERVER")
    booklore_user = os.environ.get("BOOKLORE_USER")
    booklore_pass = os.environ.get("BOOKLORE_PASSWORD")
    if not all([booklore_url, booklore_user, booklore_pass]): return False
    
    try:
        booklore_url = booklore_url.rstrip('/')
        r_login = requests.post(f"{booklore_url}/api/v1/auth/login", json={"username": booklore_user, "password": booklore_pass})
        if r_login.status_code != 200: return False
        headers = {"Authorization": f"Bearer {r_login.json().get('refreshToken')}"}
        
        r_books = requests.get(f"{booklore_url}/api/v1/books", headers=headers)
        target_book = next((b for b in r_books.json() if b.get('fileName') == ebook_filename), None)
        if not target_book: return False
        
        r_shelves = requests.get(f"{booklore_url}/api/v1/shelves", headers=headers)
        target_shelf = next((s for s in r_shelves.json() if s.get('name') == shelf_name), None)
        
        if not target_shelf:
            r_create = requests.post(f"{booklore_url}/api/v1/shelves", headers=headers, json={"name": shelf_name, "icon": "📚", "iconType": "PRIME_NG"})
            if r_create.status_code == 201: target_shelf = r_create.json()
            else: return False
            
        r_assign = requests.post(f"{booklore_url}/api/v1/books/shelves", headers=headers, json={"bookIds": [target_book['id']], "shelvesToAssign": [target_shelf['id']], "shelvesToUnassign": []})
        if r_assign.status_code in [200, 201, 204]:
            logger.info(f"🏷️ Added '{sanitize_log_data(ebook_filename)}' to Booklore Shelf: {shelf_name}")
            return True
        return False
    except: return False

# ---------------- ROUTES ----------------
@app.route('/')
def index():
    """Dashboard"""
    db = db_handler.load(default={"mappings": []})
    all_states = state_handler.load(default={})

    integrations = {
        'audiobookshelf': True,
        'kosync': manager.kosync_client.is_configured(),
        'storyteller': manager.storyteller_db.check_connection() if hasattr(manager.storyteller_db, 'check_connection') else True,
        'booklore': manager.booklore_client.check_connection() if hasattr(manager.booklore_client, 'check_connection') else False,
        'hardcover': bool(manager.hardcover_client.token)
    }

    mappings = db.get('mappings', [])
    
    total_duration = 0
    total_listened = 0
    
    for mapping in mappings:
        abs_id = mapping.get('abs_id')
        
        # 1. Initialize defaults (Safe for Template)
        if 'unified_progress' not in mapping: mapping['unified_progress'] = 0
        if 'duration' not in mapping: mapping['duration'] = 0
        
        # We start these at 0, but will try to overwrite them below
        mapping.setdefault('kosync_progress', 0)
        mapping.setdefault('storyteller_progress', 0)
        mapping.setdefault('booklore_progress', 0)
        mapping.setdefault('abs_progress', 0)

        # 2. POPULATE STATS (Try DB first, Fallback to State)
        # Check if we have state data for this book
        if abs_id in all_states:
            state = all_states[abs_id]
            
            # Helper to get % from state (0.75 -> 75.0)
            def get_pct(key):
                return round(state.get(key, 0) * 100, 1)

            # If the DB is empty/zero, use the State file for everything
            if mapping.get('unified_progress', 0) == 0:
                mapping['kosync_progress'] = get_pct('kosync_pct')
                mapping['storyteller_progress'] = get_pct('storyteller_pct')
                mapping['booklore_progress'] = get_pct('booklore_pct')
                mapping['abs_progress'] = state.get('abs_ts', 0)

                # Sanity check: ABS timestamp may be stored in milliseconds in the state file.
                # If it's a very large value, convert milliseconds -> seconds. Keep the value
                # as seconds in the mapping so the UI can show time like 08:00:00.
                if mapping['abs_progress'] > 1_000_000:
                    mapping['abs_progress'] = mapping['abs_progress'] / 1000.0

                # Compute percentage value for unified progress without overwriting abs_progress.
                duration = mapping.get('duration', 0)
                if duration and duration > 0:
                    abs_pct_value = min((mapping['abs_progress'] / duration) * 100.0, 100.0)
                else:
                    # If duration unknown, use the percentage stored in the state (abs_pct) and scale to 0-100.
                    abs_pct_value = min(state.get('abs_pct', 0) * 100.0, 100.0)

                # Recalculate Unified Progress from the max of these (use abs_pct_value)
                max_p = max(
                    mapping['kosync_progress'],

                    mapping['storyteller_progress'],
                    mapping['booklore_progress'],
                    abs_pct_value
                )
                if max_p > 0:
                    mapping['unified_progress'] = min(max_p, 100.0)

        # 3. Calculate Totals for Top Bar
        duration = mapping.get('duration', 0)
        progress_pct = mapping.get('unified_progress', 0)

        if duration > 0:
            total_duration += duration
            total_listened += (progress_pct / 100.0) * duration

        # 4. Last Sync Time
        book_state = all_states.get(abs_id, {})
        last_updated = book_state.get('last_updated', 0)
        if last_updated > 0:
            diff = time.time() - last_updated
            if diff < 60: mapping['last_sync'] = f"{int(diff)}s ago"
            elif diff < 3600: mapping['last_sync'] = f"{int(diff // 60)}m ago"
            else: mapping['last_sync'] = f"{int(diff // 3600)}h ago"
        else:
            mapping['last_sync'] = "Never"
            
        if abs_id:
             mapping['cover_url'] = f"{manager.abs_client.base_url}/api/items/{abs_id}/cover?token={manager.abs_client.token}"

    if total_duration > 0:
        overall_progress = round((total_listened / total_duration) * 100, 1)
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

    return render_template('book_linker.html', book_name=book_name, ebook_matches=ebook_matches, audiobook_matches=audiobook_matches, stats=stats, message=message, is_error=is_error,linker_books_dir=str(LINKER_BOOKS_DIR), processing_dir=str(DEST_BASE),storyteller_ingest=str(STORYTELLER_INGEST))
                         

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
        audiobooks = manager.abs_client.get_all_audiobooks()
        selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
        if not selected_ab: return "Audiobook not found", 404
        
        # Get booklore_id if available for API-based hash computation
        booklore_id = None
        if manager.booklore_client.is_configured():
            book = manager.booklore_client.find_book_by_filename(ebook_filename)
            if book:
                booklore_id = book.get('id')

        # Compute KOSync ID (Booklore API first, filesystem fallback)
        kosync_doc_id = get_kosync_id_for_ebook(ebook_filename, booklore_id)
        if not kosync_doc_id:
            logger.warning(f"Cannot compute KOSync ID for '{sanitize_log_data(ebook_filename)}': File not found in Booklore or filesystem")
            return "Could not compute KOSync ID for ebook", 404
            
        mapping = {"abs_id": abs_id, "abs_title": manager._get_abs_title(selected_ab), "ebook_filename": ebook_filename, "kosync_doc_id": kosync_doc_id, "transcript_file": None, "status": "pending"}
        def add_mapping(db):
            db['mappings'] = [m for m in db.get('mappings', []) if m['abs_id'] != abs_id]
            db['mappings'].append(mapping)
            return db
        db_handler.update(add_mapping, default={"mappings": []})
        add_to_abs_collection(manager.abs_client, abs_id)
        add_to_booklore_shelf(ebook_filename)
        manager.storyteller_db.add_to_collection(ebook_filename)
        return redirect(url_for('index'))

    search = request.args.get('search', '').strip().lower()
    audiobooks, ebooks = [], []
    if search:
        audiobooks = manager.abs_client.get_all_audiobooks()
        audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
        for ab in audiobooks: ab['cover_url'] = f"{manager.abs_client.base_url}/api/items/{ab['id']}/cover?token={manager.abs_client.token}"
        
        # Use new search method
        ebooks = get_searchable_ebooks(search)
        
    return render_template('match.html', audiobooks=audiobooks, ebooks=ebooks, search=search, get_title=manager._get_abs_title)

@app.route('/batch-match', methods=['GET', 'POST'])
def batch_match():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_to_queue':
            session.setdefault('queue', [])
            abs_id = request.form.get('audiobook_id')
            ebook_filename = request.form.get('ebook_filename')
            audiobooks = manager.abs_client.get_all_audiobooks()
            selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
            if selected_ab and ebook_filename:
                if not any(item['abs_id'] == abs_id for item in session['queue']):
                    session['queue'].append({"abs_id": abs_id, "abs_title": manager._get_abs_title(selected_ab), "ebook_filename": ebook_filename, "cover_url": f"{manager.abs_client.base_url}/api/items/{abs_id}/cover?token={manager.abs_client.token}"})
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
            db = db_handler.load(default={"mappings": []})
            for item in session.get('queue', []):
                ebook_filename = item['ebook_filename']
                
                # Get booklore_id if available for API-based hash computation
                booklore_id = None
                if manager.booklore_client.is_configured():
                    book = manager.booklore_client.find_book_by_filename(ebook_filename)
                    if book:
                        booklore_id = book.get('id')

                # Compute KOSync ID (Booklore API first, filesystem fallback)
                kosync_doc_id = get_kosync_id_for_ebook(ebook_filename, booklore_id)
                if not kosync_doc_id:
                    logger.warning(f"Could not compute KOSync ID for {sanitize_log_data(ebook_filename)}, skipping")
                    continue
                    
                mapping = {"abs_id": item['abs_id'], "abs_title": item['abs_title'], "ebook_filename": ebook_filename, "kosync_doc_id": kosync_doc_id, "transcript_file": None, "status": "pending"}
                db['mappings'] = [m for m in db['mappings'] if m['abs_id'] != item['abs_id']]
                db['mappings'].append(mapping)
                add_to_abs_collection(manager.abs_client, item['abs_id'])
                add_to_booklore_shelf(item['ebook_filename'])
                manager.storyteller_db.add_to_collection(item['ebook_filename'])
            db_handler.save(db)
            session['queue'] = []
            session.modified = True
            return redirect(url_for('index'))
            
    search = request.args.get('search', '').strip().lower()
    audiobooks, ebooks = [], []
    if search:
        audiobooks = manager.abs_client.get_all_audiobooks()
        audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
        for ab in audiobooks: ab['cover_url'] = f"{manager.abs_client.base_url}/api/items/{ab['id']}/cover?token={manager.abs_client.token}"
        
        # Use new search method
        ebooks = get_searchable_ebooks(search)
        ebooks.sort(key=lambda x: x.name.lower())
        
    return render_template('batch_match.html', audiobooks=audiobooks, ebooks=ebooks, queue=session.get('queue', []), search=search, get_title=manager._get_abs_title)

@app.route('/delete/<abs_id>', methods=['POST'])
def delete_mapping(abs_id):
    db = db_handler.load(default={"mappings": []})
    mapping = next((m for m in db.get('mappings', []) if m['abs_id'] == abs_id), None)
    if mapping:
        if mapping.get('transcript_file'):
            try: Path(mapping['transcript_file']).unlink()
            except: pass
    def remove_mapping(db):
        db['mappings'] = [m for m in db.get('mappings', []) if m['abs_id'] != abs_id]
        return db
    db_handler.update(remove_mapping, default={"mappings": []})
    def remove_state(state):
        if abs_id in state: del state[abs_id]
        return state
    state_handler.update(remove_state, default={})
    return redirect(url_for('index'))

@app.route('/clear-progress/<abs_id>', methods=['POST'])
def clear_progress(abs_id):
    """Clear progress for a mapping by setting all systems to 0%"""
    db = db_handler.load(default={"mappings": []})
    mapping = next((m for m in db.get('mappings', []) if m['abs_id'] == abs_id), None)
    
    if not mapping:
        logger.warning(f"Cannot clear progress: mapping not found for {abs_id}")
        return redirect(url_for('index'))
    
    try:
        # Reset progress to 0 in all three systems
        logger.info(f"Clearing progress for {sanitize_log_data(mapping.get('abs_title', abs_id))}")
        
        # ABS: Set to 0 seconds
        manager.abs_client.update_progress(abs_id, 0)
        logger.info(f"  ✓ ABS progress cleared")
        
        # KOSync: Set to 0%
        kosync_id = mapping.get('kosync_doc_id')
        if kosync_id:
            manager.kosync_client.update_progress(kosync_id, 0.0)
            logger.info(f"  ✓ KOSync progress cleared")
        
        # Storyteller: Set to 0%
        ebook_filename = mapping.get('ebook_filename')
        if ebook_filename:
            manager.storyteller_db.update_progress(ebook_filename, 0.0)
            logger.info(f"  ✓ Storyteller progress cleared")

        # Booklore: Set to 0%
        if ebook_filename:
            manager.booklore_client.update_progress(ebook_filename, 0.0)
            logger.info(f"  ✓ Booklore progress cleared")

        # Clear the last state so next sync will properly propagate the 0%
        state = state_handler.load(default={})
        if abs_id in state:
            del state[abs_id]
            state_handler.save(state)
            logger.info(f"  ✓ Last state cleared")
        
        logger.info(f"✅ Progress cleared successfully for {sanitize_log_data(mapping.get('abs_title', abs_id))}")
        
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

    # Update DB
    def update_map(db):
        for m in db.get('mappings', []):
            if m.get('abs_id') == abs_id:
                m['hardcover_book_id'] = book_data['book_id']
                m['hardcover_edition_id'] = book_data.get('edition_id')
                m['hardcover_pages'] = book_data.get('pages')
                m['hardcover_title'] = book_data.get('title')
        return db

    if db_handler.update(update_map):
        # Force status to 'Want to Read' (1)
        try:
            manager.hardcover_client.update_status(book_data['book_id'], 1, book_data.get('edition_id'))
        except Exception as e:
            logger.warning(f"Failed to set Hardcover status: {e}")
        flash(f"✅ Linked Hardcover: {book_data.get('title')}", "success")
    else:
        flash("❌ Database update failed", "error")

    return redirect(url_for('index'))

@app.route('/api/status')
def api_status():
    return jsonify(db_handler.load(default={"mappings": []}))

@app.route('/view_log')
def view_log():
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()[-300:]
        return "<pre>" + "\n".join(html.escape(l) for l in lines) + "</pre>"
    except: return "Log not available"

if __name__ == '__main__':
    logger.info("=== Unified ABS Manager Started ===")

    # Check ebook source configuration
    booklore_configured = manager.booklore_client.is_configured()
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

    logger.info(f"Book Linker monitoring interval: {MONITOR_INTERVAL} seconds")
    monitor_thread = threading.Thread(target=monitor_readaloud_files, daemon=True)
    monitor_thread.start()
    app.run(host='0.0.0.0', port=5757, debug=False)
# [END FILE]
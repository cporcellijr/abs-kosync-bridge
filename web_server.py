from flask import Flask, render_template, request, redirect, url_for, jsonify, session
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

# ---------------- APP SETUP ----------------

app = Flask(__name__)
app.secret_key = "kosync-queue-secret-unified-app"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

manager = SyncManager()

db_handler = JsonDB("/data/mapping_db.json")
state_handler = JsonDB("/data/last_state.json")
suggestions_handler = JsonDB("/data/suggestions.json")  # RESTORED

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
    file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
    logger.addHandler(file_handler)

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
            if full_path and Path(full_path).exists():
                src_path = Path(full_path)
            elif full_path:
                for base_part in [str(ABS_AUDIO_ROOT), "/audiobooks", "audiobooks"]:
                    if base_part in full_path:
                        rel_part = full_path.split(base_part)[-1].lstrip("\\/")
                        test_path = ABS_AUDIO_ROOT / rel_part
                        if test_path.exists():
                            src_path = test_path
                            break
            if not src_path and filename:
                for found_file in ABS_AUDIO_ROOT.rglob(filename):
                    src_path = found_file
                    break
            
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
        requests.post(add_url, headers=abs_client.headers, json={"id": item_id})
        return True
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
            
        requests.post(f"{booklore_url}/api/v1/books/shelves", headers=headers, json={"bookIds": [target_book['id']], "shelvesToAssign": [target_shelf['id']], "shelvesToUnassign": []})
        return True
    except: return False

# ---------------- ROUTES ----------------

@app.route('/')
def index():
    """Dashboard"""
    db = db_handler.load(default={"mappings": []})
    state = state_handler.load(default={})
    suggestions = suggestions_handler.load(default={}) # Load suggestions
    
    mappings = db.get('mappings', [])
    for mapping in mappings:
        abs_id = mapping.get('abs_id')
        kosync_id = mapping.get('kosync_doc_id')
        ebook_filename = mapping.get('ebook_filename')
        try:
            abs_progress = manager.abs_client.get_progress(abs_id)
            kosync_progress = manager.kosync_client.get_progress(kosync_id)
            storyteller_progress, _ = manager.storyteller_db.get_progress(ebook_filename)
            if storyteller_progress is None: storyteller_progress = 0.0
            
            mapping['storyteller_progress'] = storyteller_progress * 100
            mapping['abs_progress'] = abs_progress
            mapping['kosync_progress'] = kosync_progress * 100
            mapping['unified_progress'] = max(mapping['kosync_progress'], mapping['storyteller_progress'])

            book_state = state.get(abs_id, {})
            last_updated = book_state.get('last_updated', 0)
            if last_updated > 0:
                diff = time.time() - last_updated
                if diff < 60: mapping['last_sync'] = f"{int(diff)}s ago"
                elif diff < 3600: mapping['last_sync'] = f"{int(diff // 60)}m ago"
                else: mapping['last_sync'] = f"{int(diff // 3600)}h ago"
            else: mapping['last_sync'] = "Never"
            
            mapping['cover_url'] = f"{manager.abs_client.base_url}/api/items/{abs_id}/cover?token={manager.abs_client.token}"
        except Exception as e:
            logger.error(f"Error fetching progress: {e}")
            mapping['last_sync'] = "Error"
            mapping['cover_url'] = None

    # CRASH FIX: Calculate suggestion count and pass to template
    suggestion_count = sum(1 for s in suggestions.values() if s.get('state') == 'pending')
    
    return render_template('index.html', mappings=mappings, suggestion_count=suggestion_count)

# RESTORED SUGGESTION ROUTES
@app.route('/suggestions')
def suggestions_page():
    suggestions = suggestions_handler.load(default={})
    pending = {k: v for k, v in suggestions.items() if v.get('state') == 'pending'}
    sorted_sugg = sorted(pending.items(), key=lambda x: x[1].get('score', 0), reverse=True)
    return render_template('suggestions.html', suggestions=sorted_sugg)

@app.route('/suggestions/accept/<key>', methods=['POST'])
def accept_suggestion(key):
    suggestions = suggestions_handler.load(default={})
    if key not in suggestions: return "Suggestion not found", 404
    
    sugg = suggestions[key]
    abs_id, abs_title, ebook_filename = None, None, None
    
    if sugg.get('match_type') == 'ebook':
        abs_id = sugg['source_id']
        abs_title = sugg['source_title']
        ebook_filename = sugg['match_filename']
    elif sugg.get('match_type') == 'audiobook':
        abs_id = sugg['match_id']
        abs_title = sugg['match_title']
        ebook_filename = sugg.get('source_filename')
    
    if not ebook_filename: return "Ebook filename missing", 400
    ebook_path = find_ebook_file(ebook_filename)
    if not ebook_path: return "Ebook file missing", 404
    
    kosync_doc_id = manager.ebook_parser.get_kosync_id(ebook_path)
    mapping = {
        "abs_id": abs_id, "abs_title": abs_title, "ebook_filename": ebook_filename,
        "kosync_doc_id": kosync_doc_id, "transcript_file": None, "status": "pending"
    }
    
    def txn(db):
        db['mappings'] = [m for m in db.get('mappings', []) if m['abs_id'] != abs_id]
        db['mappings'].append(mapping)
        return db
    db_handler.update(txn, default={"mappings": []})
    
    sugg['state'] = 'accepted'
    suggestions_handler.save(suggestions)
    
    add_to_abs_collection(manager.abs_client, abs_id)
    add_to_booklore_shelf(ebook_filename)
    manager.storyteller_db.add_to_collection(ebook_filename)
    return redirect(url_for('suggestions_page'))

@app.route('/suggestions/dismiss/<key>', methods=['POST'])
def dismiss_suggestion(key):
    suggestions = suggestions_handler.load(default={})
    if key in suggestions:
        suggestions[key]['state'] = 'dismissed'
        suggestions_handler.save(suggestions)
    return redirect(url_for('suggestions_page'))

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

    return render_template('book_linker.html', book_name=book_name, ebook_matches=ebook_matches, audiobook_matches=audiobook_matches, stats=stats, message=message, is_error=is_error)

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
        ebook_path = find_ebook_file(ebook_filename)
        if not ebook_path: return "Ebook not found", 404
        kosync_doc_id = manager.ebook_parser.get_kosync_id(ebook_path)
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
        ebooks = list(EBOOK_DIR.glob("**/*.epub"))
        audiobooks = [ab for ab in audiobooks if search in manager._get_abs_title(ab).lower()]
        ebooks = [eb for eb in ebooks if search in eb.name.lower()]
        for ab in audiobooks: ab['cover_url'] = f"{manager.abs_client.base_url}/api/items/{ab['id']}/cover?token={manager.abs_client.token}"
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
                ebook_path = find_ebook_file(item['ebook_filename'])
                if not ebook_path: continue
                kosync_doc_id = manager.ebook_parser.get_kosync_id(ebook_path)
                mapping = {"abs_id": item['abs_id'], "abs_title": item['abs_title'], "ebook_filename": item['ebook_filename'], "kosync_doc_id": kosync_doc_id, "transcript_file": None, "status": "pending"}
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
        ebooks = list(EBOOK_DIR.glob("**/*.epub"))
        audiobooks = [ab for ab in audiobooks if search in manager._get_abs_title(ab).lower()]
        ebooks = [eb for eb in ebooks if search in eb.name.lower()]
        for ab in audiobooks: ab['cover_url'] = f"{manager.abs_client.base_url}/api/items/{ab['id']}/cover?token={manager.abs_client.token}"
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
    logger.info(f"Book Linker monitoring interval: {MONITOR_INTERVAL} seconds")
    monitor_thread = threading.Thread(target=monitor_readaloud_files, daemon=True)
    monitor_thread.start()
    logger.info("+ Started readaloud file monitoring thread")
    app.run(host='0.0.0.0', port=5757, debug=False)
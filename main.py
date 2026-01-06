# [START FILE: abs-kosync-enhanced/main.py]
import os
import time
import json
import schedule
import logging
import threading
from pathlib import Path
from zipfile import ZipFile
import lxml.etree as ET

from json_db import JsonDB
from api_clients import ABSClient, KoSyncClient
from hardcover_client import HardcoverClient
from booklore_client import BookloreClient
from ebook_utils import EbookParser

# FIX: Safer import logic that doesn't crash immediately
StorytellerClientClass = None
try:
    from storyteller_api import StorytellerDBWithAPI
    StorytellerClientClass = StorytellerDBWithAPI
except ImportError:
    pass

if not StorytellerClientClass:
    try:
        from storyteller_db import StorytellerDB as StorytellerClientClass
    except ImportError:
        StorytellerClientClass = None

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

DATA_DIR = Path("/data")
BOOKS_DIR = Path("/books")
DB_FILE = DATA_DIR / "mapping_db.json"
STATE_FILE = DATA_DIR / "last_state.json"

class SyncManager:
    def __init__(self):
        logger.info("=== Sync Manager Starting (Release 6.0 - Precision XPath) ===")
        self.abs_client = ABSClient()
        self.kosync_client = KoSyncClient()
        self.hardcover_client = HardcoverClient()
        # FIX: Wrap instantiation in try/except to prevent crash on init failure
        try:
            if StorytellerClientClass:
                self.storyteller_db = StorytellerClientClass()
            else:
                raise ImportError("No Storyteller client available")
        except Exception as e:
            logger.error(f"⚠️ Failed to init Storyteller client: {e}. Storyteller sync will be DISABLED.")
            # Minimal dummy class to prevent crashes in the rest of the app
            class DummyST:
                def check_connection(self): return False
                def get_progress_with_fragment(self, *args): return None, None, None, None
                def update_progress(self, *args): return False
            self.storyteller_db = DummyST()
        self.booklore_client = BookloreClient()
        self._transcriber = None
        self.epub_cache_dir = DATA_DIR / "epub_cache"
        self.ebook_parser = EbookParser(BOOKS_DIR, epub_cache_dir=self.epub_cache_dir)
        self.db_handler = JsonDB(DB_FILE)
        self.state_handler = JsonDB(STATE_FILE)
        self.db = self.db_handler.load(default={"mappings": []})
        self.state = self.state_handler.load(default={})

        self._job_queue = []
        self._job_lock = threading.Lock()
        self._job_thread = None

        self.delta_abs_thresh = float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60))
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0
        self.startup_checks()
        self.cleanup_stale_jobs()

    @property
    def transcriber(self):
        if self._transcriber is None:
            from transcriber import AudioTranscriber
            self._transcriber = AudioTranscriber(DATA_DIR)
        return self._transcriber

    def startup_checks(self):
        self.abs_client.check_connection()
        self.kosync_client.check_connection()
        self.storyteller_db.check_connection()
        self.booklore_client.check_connection()

    def cleanup_stale_jobs(self):
        """Reset jobs that were interrupted mid-process on restart."""
        changed = False
        for mapping in self.db.get('mappings', []):
            status = mapping.get('status')
            if status == 'crashed':
                mapping['status'] = 'active'
                changed = True
            elif status == 'processing':
                # Job was interrupted mid-process, retry it
                logger.info(f"[JOB] Recovering interrupted job: {mapping.get('abs_title')}")
                mapping['status'] = 'failed_retry_later'
                mapping['last_error'] = 'Interrupted by restart'
                changed = True
        if changed: self.db_handler.save(self.db)

    def _get_abs_title(self, ab):
        media = ab.get('media', {})
        metadata = media.get('metadata', {})
        return metadata.get('title') or ab.get('name', 'Unknown')

    def _automatch_hardcover(self, mapping):
        if not self.hardcover_client.token: return
        item = self.abs_client.get_item_details(mapping['abs_id'])
        if not item: return

        meta = item.get('media', {}).get('metadata', {})
        match = None

        # Extract metadata fields for clarity
        isbn = meta.get('isbn')
        asin = meta.get('asin')
        title = meta.get('title')
        author = meta.get('authorName')


        if isbn:
            match = self.hardcover_client.search_by_isbn(isbn)
        if not match and asin:
            match = self.hardcover_client.search_by_isbn(asin)
        if not match and title and author:
            match = self.hardcover_client.search_by_title_author(title, author)
        if not match and title:

            match = self.hardcover_client.search_by_title_author(title, "")
        if match:
             mapping.update({'hardcover_book_id': match['book_id'], 'hardcover_edition_id': match.get('edition_id'), 'hardcover_pages': match.get('pages')})
             self.db_handler.save(self.db)
             # UPDATED: Set status to 1 (Want to Read) initially
             self.hardcover_client.update_status(match['book_id'], 1, match.get('edition_id'))
             logger.info(f"📚 Hardcover: Matched '{meta.get('title')}' -> Want to Read")

    def _sync_to_hardcover(self, mapping, percentage):
        # 1. Basic checks
        if not self.hardcover_client.token: return
        if not mapping.get('hardcover_book_id'): return

        # 2. DEFINE 'ub' BEFORE USING IT
        ub = self.hardcover_client.get_user_book(mapping['hardcover_book_id'])

        # 3. Now it is safe to check 'if ub:'
        if ub:
            total_pages = mapping.get('hardcover_pages') or 0

            # SAFETY: If total_pages is zero we cannot compute a valid page number
            if total_pages == 0:
                logger.warning(f"⚠️ Hardcover Sync Skipped: {mapping.get('abs_title')} has 0 pages.")
                return

            page_num = int(total_pages * percentage)
            is_finished = percentage > 0.99

            current_status = ub.get('status_id')

            # Handle Status Changes
            # If Finished, prefer marking as Read (3) first
            if is_finished and current_status != 3:
                self.hardcover_client.update_status(mapping['hardcover_book_id'], 3, mapping.get('hardcover_edition_id'))
                logger.info(f"📚 Hardcover: Marked '{mapping.get('abs_title', 'Unknown')}' as finished")
                current_status = 3

            # If progress > 2% and currently "Want to Read" (1), switch to "Currently Reading" (2)
            elif percentage > 0.02 and current_status == 1:
                self.hardcover_client.update_status(mapping['hardcover_book_id'], 2, mapping.get('hardcover_edition_id'))
                logger.info(f"📚 Hardcover: Started '{mapping.get('abs_title', 'Unknown')}' (>2%)")
                current_status = 2

            # Now it's safe to update progress (Hardcover rejects page updates for Want to Read)
            self.hardcover_client.update_progress(
                ub['id'], 
                page_num, 
                edition_id=mapping.get('hardcover_edition_id'), 
                is_finished=is_finished, 
                current_percentage=percentage
            )

    def _abs_to_percentage(self, abs_seconds, transcript_path):
        try:
            with open(transcript_path, 'r') as f: 
                data = json.load(f)
                dur = data[-1]['end'] if isinstance(data, list) else data.get('duration', 0)
                return min(max(abs_seconds / dur, 0.0), 1.0) if dur > 0 else None
        except: return None

    def _get_local_epub(self, ebook_filename, working_dir=None):
        """
        Get local path to EPUB file, downloading from Booklore if necessary.
        """
        # First, try to find on filesystem
        filesystem_matches = list(BOOKS_DIR.glob(f"**/{ebook_filename}"))
        if filesystem_matches:
            logger.info(f"📚 Found EPUB on filesystem: {filesystem_matches[0]}")
            return filesystem_matches[0]

        # Check persistent EPUB cache
        self.epub_cache_dir.mkdir(parents=True, exist_ok=True)
        cached_path = self.epub_cache_dir / ebook_filename
        if cached_path.exists():
            logger.info(f"📚 Found EPUB in cache: {cached_path}")
            return cached_path

        # Try to download from Booklore API
        # Note: We use hasattr to prevent crashes if BookloreClient wasn't updated with these methods yet
        if hasattr(self.booklore_client, 'is_configured') and self.booklore_client.is_configured():
            book = self.booklore_client.find_book_by_filename(ebook_filename)
            if book:
                logger.info(f"📥 Downloading EPUB from Booklore: {ebook_filename}")
                if hasattr(self.booklore_client, 'download_book'):
                    content = self.booklore_client.download_book(book['id'])
                    if content:
                        with open(cached_path, 'wb') as f:
                            f.write(content)
                        logger.info(f"✅ Downloaded EPUB to cache: {cached_path}")
                        return cached_path
                    else:
                        logger.error(f"Failed to download EPUB content from Booklore")
            else:
                logger.error(f"EPUB not found in Booklore: {ebook_filename}")
        else:
            if not filesystem_matches:
                 logger.error(f"EPUB not found on filesystem and Booklore not configured")

        return None

    def check_pending_jobs(self):
        """
        Check for pending jobs and run them in a BACKGROUND thread
        so we don't block the sync cycle.
        """
        # 1. If a job is already running, let it finish.
        if self._job_thread and self._job_thread.is_alive():
            return

        # 2. Find ONE pending job to start
        # Reload DB to ensure we have fresh status
        self.db = self.db_handler.load(default={"mappings": []})
        target_mapping = None
        
        # Check for 'pending' jobs (prioritize them)
        for mapping in self.db.get('mappings', []):
            if mapping.get('status') == 'pending':
                target_mapping = mapping
                break
        
        # If no pending jobs, check for retries
        if not target_mapping:
            max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))
            retry_delay_mins = int(os.getenv("JOB_RETRY_DELAY_MINS", 15))
            
            for mapping in self.db.get('mappings', []):
                if mapping.get('status') == 'failed_retry_later':
                    last_attempt = mapping.get('last_attempt', 0)
                    retry_count = mapping.get('retry_count', 0)
                    
                    if retry_count >= max_retries:
                        continue # Skip permanent failures
                        
                    if time.time() - last_attempt > retry_delay_mins * 60:
                        target_mapping = mapping
                        logger.info(f"[JOB] Retrying ({retry_count + 1}/{max_retries}): {mapping.get('abs_title')}")
                        break

        if not target_mapping:
            return

        # 3. Mark as 'processing' immediately so we don't pick it up again
        logger.info(f"[JOB] Starting background transcription: {target_mapping.get('abs_title')}")
        
        # Atomic update to mark processing
        def set_processing(db):
            for m in db.get('mappings', []):
                if m['abs_id'] == target_mapping['abs_id']:
                    m['status'] = 'processing'
                    m['last_attempt'] = time.time()
            return db
        self.db_handler.update(set_processing)

        # 4. Launch the heavy work in a separate thread
        self._job_thread = threading.Thread(
            target=self._run_background_job, 
            args=(target_mapping,),
            daemon=True
        )
        self._job_thread.start()

    def _run_background_job(self, mapping_data):
        """
        Threaded worker that handles transcription without blocking the main loop.
        """
        abs_id = mapping_data['abs_id']
        abs_title = mapping_data.get('abs_title', 'Unknown')
        ebook_filename = mapping_data['ebook_filename']
        max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))

        try:
            # --- Heavy Lifting (Blocks this thread, but not the Main thread) ---
            # Step 1: Get EPUB file
            epub_path = self._get_local_epub(ebook_filename)
            if not epub_path:
                raise FileNotFoundError(f"Could not locate or download: {ebook_filename}")

            # Step 2: Download and transcribe audio
            audio_files = self.abs_client.get_audio_files(abs_id)
            transcript_path = self.transcriber.process_audio(abs_id, audio_files)

            # Step 3: Parse EPUB
            self.ebook_parser.extract_text_and_map(epub_path)
            
            # --- Atomic Success Update ---
            def success_update(db):
                for m in db.get('mappings', []):
                    if m['abs_id'] == abs_id:
                        m['transcript_file'] = str(transcript_path)
                        m['status'] = 'active'
                        m['retry_count'] = 0
                return db
            
            self.db_handler.update(success_update)

            self.db = self.db_handler.load()
            
            # Trigger Hardcover Match (using fresh DB data)
            final_db = self.db_handler.load()
            final_mapping = next((m for m in final_db.get('mappings', []) if m['abs_id'] == abs_id), None)
            if final_mapping:
                self._automatch_hardcover(final_mapping)
            
            logger.info(f"[JOB] Completed: {abs_title}")

        except Exception as e:
            logger.error(f"[FAIL] {abs_title}: {e}")
            # Atomic Failure Update
            def fail_update(db):
                for m in db.get('mappings', []):
                    if m['abs_id'] == abs_id:
                        # Increment retry count
                        curr_retries = m.get('retry_count', 0) + 1
                        m['retry_count'] = curr_retries
                        m['last_error'] = str(e)
                        
                        if curr_retries >= max_retries:
                             m['status'] = 'failed_permanent'
                             logger.warning(f"[JOB] {abs_title}: Max retries exceeded")
                        else:
                             m['status'] = 'failed_retry_later'
                return db
            self.db_handler.update(fail_update)
      
            self.db = self.db_handler.load()

    def get_text_from_storyteller_fragment(self, ebook_filename, href, fragment_id):
        return self.ebook_parser.resolve_locator_id(ebook_filename, href, fragment_id)

    def sync_cycle(self):
        self.db = self.db_handler.load(default={"mappings": []})
        self.state = self.state_handler.load(default={}) 
        
        # --- NEW: Bulk Fetch ABS Progress ---
        abs_in_progress = {}
        try:
            for item in self.abs_client.get_in_progress():
                abs_in_progress[item['id']] = item
        except Exception as e:
            logger.error(f"Failed to bulk fetch progress: {e}")
            abs_in_progress = {}

        active_books = [m for m in self.db.get('mappings', []) if m.get('status') == 'active']
        if active_books: logger.info(f"🔄 Sync cycle starting - {len(active_books)} active book(s)")
        
        db_dirty = False 

        for mapping in self.db.get('mappings', []):
            if mapping.get('status') != 'active': continue
            
            abs_id = mapping['abs_id']

            # --- NEW: Update Web Stats ---
            abs_item = abs_in_progress.get(abs_id)
            if abs_item:
                new_prog = abs_item.get('progress', 0) * 100
                new_dur = abs_item.get('duration', 0)
                
                if mapping.get('unified_progress') != new_prog or mapping.get('duration') != new_dur:
                    mapping['unified_progress'] = new_prog
                    mapping['duration'] = new_dur
                    db_dirty = True
            
        
        active_books = [m for m in self.db.get('mappings', []) if m.get('status') == 'active']
        if active_books: logger.info(f"🔄 Sync cycle starting - {len(active_books)} active book(s)")
        
        for mapping in self.db.get('mappings', []):
            if mapping.get('status') != 'active': continue
            abs_id, ko_id, epub = mapping['abs_id'], mapping['kosync_doc_id'], mapping['ebook_filename']
            
            try:
                # 1. Fetch raw states
                st_pct, st_ts, st_href, st_frag = self.storyteller_db.get_progress_with_fragment(epub)
                bl_pct, bl_cfi = self.booklore_client.get_progress(epub)
                abs_ts = self.abs_client.get_progress(abs_id)
             

                # UPDATED: KoSync now returns tuple (pct, xpath)
                ko_pct, ko_xpath = (0.0, None)
                if self.kosync_client.is_configured():
                    ko_pct, ko_xpath = self.kosync_client.get_progress(ko_id)

                if abs_ts is None: continue # ABS offline
                
                abs_pct = self._abs_to_percentage(abs_ts, mapping.get('transcript_file'))
                if abs_ts > 0 and abs_pct is None: continue # Invalid transcript

                if ko_pct is None: ko_pct = 0.0
                if st_pct is None: st_pct = 0.0
                if bl_pct is None: bl_pct = 0.0

                prev = self.state.get(abs_id, {})
                vals = {'ABS': abs_pct or 0, 'KOSYNC': ko_pct, 'STORYTELLER': st_pct, 'BOOKLORE': bl_pct}

                changed = False
                if abs(vals['ABS'] - prev.get('abs_pct', 0)) > 0.01: changed = True
                if abs(vals['KOSYNC'] - prev.get('kosync_pct', 0)) > self.delta_kosync_thresh: changed = True
                if abs(vals['STORYTELLER'] - prev.get('storyteller_pct', 0)) > self.delta_kosync_thresh: changed = True
                if abs(vals['BOOKLORE'] - prev.get('booklore_pct', 0)) > self.delta_kosync_thresh: changed = True
                
                if not changed:
                    if abs_id not in self.state: self.state[abs_id] = prev
                    self.state[abs_id]['last_updated'] = prev.get('last_updated', 0)
                    continue

                leader = max(vals, key=vals.get)
                
                if vals[leader] == 0.0 and max(prev.get('abs_pct', 0), prev.get('kosync_pct', 0), prev.get('booklore_pct', 0)) > 0.05:
                    logger.warning(f"🛡️   [{mapping.get('abs_title', 'Unknown')[:30]}] REGRESSION BLOCKED")
                    continue

                logger.info(f"📖 [{mapping.get('abs_title', 'Unknown')[:30]}] {leader} leads at {vals[leader]:.1%}")
                
                final_ts, final_pct = abs_ts, vals[leader]
                sync_success = False
                
                # --- LEADER LOGIC ---
                if leader == 'ABS':
                    txt = self.transcriber.get_text_at_time(mapping.get('transcript_file'), abs_ts)
                    if txt:
                        match_pct, rich_locator = self.ebook_parser.find_text_location(epub, txt, hint_percentage=abs_pct)
                        if match_pct:
                            # Send full rich locator back to KOReader (as xpath)
                            self.kosync_client.update_progress(ko_id, match_pct, None)
                            self.storyteller_db.update_progress(epub, match_pct, rich_locator)
                            self.booklore_client.update_progress(epub, match_pct, rich_locator)
                            final_pct = match_pct
                            sync_success = True

                elif leader == 'KOSYNC':
                    # NEW: Try to use XPath first!
                    txt = None
                    if ko_xpath:
                        txt = self.ebook_parser.resolve_xpath(epub, ko_xpath)
                        if txt: logger.debug(f"   📝 Using XPath text from {ko_xpath}")
                    
                    if not txt:
                        txt = self.ebook_parser.get_text_at_percentage(epub, ko_pct)
                        logger.debug(f"   📝 Using ebook text at {ko_pct:.1%} (fallback)")
                    
                    if txt:
                        ts = self.transcriber.find_time_for_text(mapping.get('transcript_file'), txt, hint_percentage=ko_pct)
                        if ts:
                            self.abs_client.update_progress(abs_id, ts)
                            match_pct, rich_locator = self.ebook_parser.find_text_location(epub, txt, hint_percentage=ko_pct)
                            if match_pct:
                                self.storyteller_db.update_progress(epub, match_pct, rich_locator)
                                self.booklore_client.update_progress(epub, match_pct, rich_locator)
                                final_pct = match_pct
                            else:
                                self.storyteller_db.update_progress(epub, ko_pct, None)
                                self.booklore_client.update_progress(epub, ko_pct, None)
                                final_pct = ko_pct
                            final_ts = ts
                            sync_success = True

                elif leader == 'STORYTELLER':
                    # Existing logic using st_href/st_frag
                    txt = self.get_text_from_storyteller_fragment(epub, st_href, st_frag) if st_frag else None
                    if not txt: txt = self.ebook_parser.get_text_at_percentage(epub, st_pct)
                    
                    if txt:
                        ts = self.transcriber.find_time_for_text(mapping.get('transcript_file'), txt, hint_percentage=st_pct)
                        if ts:
                            self.abs_client.update_progress(abs_id, ts)
                            match_pct, rich_locator = self.ebook_parser.find_text_location(epub, txt, hint_percentage=st_pct)
                            if match_pct:
                                self.kosync_client.update_progress(ko_id, match_pct, None)
                                self.booklore_client.update_progress(epub, match_pct, rich_locator)
                                final_pct = match_pct
                            else:
                                self.kosync_client.update_progress(ko_id, st_pct, None)
                                self.booklore_client.update_progress(epub, st_pct, None)
                                final_pct = st_pct
                            final_ts = ts
                            sync_success = True
                
                elif leader == 'BOOKLORE':
                    # Similar logic (if Booklore sends CFI, we could use that, but logs showed it receiving paths)
                    txt = self.ebook_parser.get_text_at_percentage(epub, bl_pct)
                    if txt:
                        ts = self.transcriber.find_time_for_text(mapping.get('transcript_file'), txt, hint_percentage=bl_pct)
                        if ts:
                            self.abs_client.update_progress(abs_id, ts)
                            match_pct, rich_locator = self.ebook_parser.find_text_location(epub, txt, hint_percentage=bl_pct)
                            if match_pct:
                                self.kosync_client.update_progress(ko_id, match_pct, None)
                                self.storyteller_db.update_progress(epub, match_pct, rich_locator)
                                final_pct = match_pct
                            else:
                                self.kosync_client.update_progress(ko_id, bl_pct, None)
                                self.storyteller_db.update_progress(epub, bl_pct, None)
                                final_pct = bl_pct
                            final_ts = ts
                            sync_success = True

                if sync_success and final_pct > 0.01:
                    if not mapping.get('hardcover_book_id'): self._automatch_hardcover(mapping)
                    if mapping.get('hardcover_book_id'): self._sync_to_hardcover(mapping, final_pct)

                self.state[abs_id] = {
                    'abs_ts': final_ts,
                    'abs_pct': self._abs_to_percentage(final_ts, mapping.get('transcript_file')) or 0,
                    'kosync_pct': final_pct,
                    'storyteller_pct': final_pct,
                    'booklore_pct': final_pct,
                    'last_updated': time.time()
                }
                self.state_handler.save(self.state)
                
            except Exception as e:
                logger.error(f"Sync error: {e}")
        if db_dirty: self.db_handler.save(self.db)

    def run_daemon(self):
        schedule.every(int(os.getenv("SYNC_PERIOD_MINS", 5))).minutes.do(self.sync_cycle)
        schedule.every(1).minutes.do(self.check_pending_jobs)
        logger.info("Daemon started.")
        self.sync_cycle()
        while True:
            schedule.run_pending()
            time.sleep(30)

if __name__ == "__main__":
    SyncManager().run_daemon()
# [END FILE]
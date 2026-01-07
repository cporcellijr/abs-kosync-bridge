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

# Logging utilities (placed at top to ensure availability during sync)
from logging_utils import sanitize_log_data, time_execution

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

# Silence noisy third-party loggers
for noisy in ('urllib3', 'requests', 'schedule', 'chardet', 'multipart', 'faster_whisper'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Use environment variable for DATA_DIR and BOOKS_DIR, defaulting to /data and /books
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BOOKS_DIR = Path(os.environ.get("BOOKS_DIR", "/books"))
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
                def is_configured(self): return False
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
        self.abs_progress_offset = float(os.getenv("ABS_PROGRESS_OFFSET_SECONDS", 0))
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
                logger.info(f"[JOB] Recovering interrupted job: {sanitize_log_data(mapping.get('abs_title'))}")
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
             logger.info(f"📚 Hardcover: '{sanitize_log_data(meta.get('title'))}' status promoted to Want to Read")

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
                logger.warning(f"⚠️ Hardcover Sync Skipped: {sanitize_log_data(mapping.get('abs_title'))} has 0 pages.")
                return

            page_num = int(total_pages * percentage)
            is_finished = percentage > 0.99

            current_status = ub.get('status_id')

            # Handle Status Changes
            # If Finished, prefer marking as Read (3) first
            if is_finished and current_status != 3:
                self.hardcover_client.update_status(mapping['hardcover_book_id'], 3, mapping.get('hardcover_edition_id'))
                logger.info(f"📚 Hardcover: '{sanitize_log_data(mapping.get('abs_title'))}' status promoted to Read")
                current_status = 3

            # If progress > 2% and currently "Want to Read" (1), switch to "Currently Reading" (2)
            elif percentage > 0.02 and current_status == 1:
                self.hardcover_client.update_status(mapping['hardcover_book_id'], 2, mapping.get('hardcover_edition_id'))
                logger.info(f"📚 Hardcover: '{sanitize_log_data(mapping.get('abs_title'))}' status promoted to Currently Reading")
                current_status = 2

            # Now it's safe to update progress (Hardcover rejects page updates for Want to Read)
            self.hardcover_client.update_progress(
                ub['id'],
                page_num,
                edition_id=mapping.get('hardcover_edition_id'),
                is_finished=is_finished,
                current_percentage=percentage
            )

    def _update_abs_progress_with_offset(self, abs_id, ts):
        """Apply offset to timestamp and update ABS progress."""
        adjusted_ts = round(ts + self.abs_progress_offset, 2)
        if self.abs_progress_offset != 0:
            logger.debug(f"   📐 Adjusted timestamp: {ts}s → {adjusted_ts}s (offset: {self.abs_progress_offset:+.1f}s)")
        abs_ok = self.abs_client.update_progress(abs_id, adjusted_ts)
        if abs_ok: logger.info("✅ ABS update successful")
        return abs_ok, adjusted_ts

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
                logger.info(f"📥 Downloading EPUB from Booklore: {sanitize_log_data(ebook_filename)}")
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
                logger.error(f"EPUB not found in Booklore: {sanitize_log_data(ebook_filename)}")
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

        # 2. Find ONE pending job to start (prioritize pending, then eligible retries)
        # Reload DB to ensure we have fresh status
        self.db = self.db_handler.load(default={"mappings": []})
        target_mapping = None
        eligible_jobs = []
        max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))
        retry_delay_mins = int(os.getenv("JOB_RETRY_DELAY_MINS", 15))

        for mapping in self.db.get('mappings', []):
            status = mapping.get('status')
            if status == 'pending':
                eligible_jobs.append(mapping)
                if not target_mapping:
                    target_mapping = mapping
            elif status == 'failed_retry_later':
                last_attempt = mapping.get('last_attempt', 0)
                retry_count = mapping.get('retry_count', 0)
                if retry_count >= max_retries:
                    continue
                if time.time() - last_attempt > retry_delay_mins * 60:
                    eligible_jobs.append(mapping)
                    if not target_mapping:
                        target_mapping = mapping

        if not target_mapping:
            return

        total_jobs = len(eligible_jobs)
        job_idx = (eligible_jobs.index(target_mapping) + 1) if total_jobs else 1

        # 3. Mark as 'processing' immediately so we don't pick it up again
        logger.info(f"[JOB {job_idx}/{total_jobs}] Starting background transcription: {sanitize_log_data(target_mapping.get('abs_title'))}")

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
            args=(target_mapping, job_idx, total_jobs),
            daemon=True
        )
        self._job_thread.start()

    def _run_background_job(self, mapping_data, job_idx=1, job_total=1):
        """
        Threaded worker that handles transcription without blocking the main loop.
        """
        abs_id = mapping_data['abs_id']
        abs_title = mapping_data.get('abs_title', 'Unknown')
        ebook_filename = mapping_data['ebook_filename']
        max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))

        # Milestone log for background job
        logger.info(f"[JOB {job_idx}/{job_total}] Processing '{sanitize_log_data(abs_title)}'...")

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

            logger.info(f"[JOB] Completed: {sanitize_log_data(abs_title)}")

        except Exception as e:
            logger.error(f"[FAIL] {sanitize_log_data(abs_title)}: {e}")
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
                             logger.warning(f"[JOB] {sanitize_log_data(abs_title)}: Max retries exceeded")
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
        if active_books: logger.debug(f"🔄 Sync cycle starting - {len(active_books)} active book(s)")

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
        if active_books: logger.debug(f"🔄 Sync cycle starting - {len(active_books)} active book(s)")

        for mapping in self.db.get('mappings', []):
            if mapping.get('status') != 'active': continue
            abs_id, ko_id, epub = mapping['abs_id'], mapping['kosync_doc_id'], mapping['ebook_filename']
            title_snip = sanitize_log_data(mapping.get('abs_title', 'Unknown'))

            try:
                # 1. Fetch raw states
                st_pct, st_ts, st_href, st_frag = self.storyteller_db.get_progress_with_fragment(epub)
                bl_pct, bl_cfi = self.booklore_client.get_progress(epub)
                abs_ts = self.abs_client.get_progress(abs_id)


                # UPDATED: KoSync now returns tuple (pct, xpath)
                ko_pct, ko_xpath = (0.0, None)
                if self.kosync_client.is_configured():
                    ko_pct, ko_xpath = self.kosync_client.get_progress(ko_id)
                    logger.debug(f"📚 [{title_snip}] KoSync response: pct={ko_pct:.1%}, xpath={ko_xpath}")
                    if ko_xpath is None:
                        logger.debug(f"⚠️ [{title_snip}] KoSync xpath is None - will use fallback text extraction")

                if abs_ts is None: continue # ABS offline

                abs_pct = self._abs_to_percentage(abs_ts, mapping.get('transcript_file'))
                if abs_ts > 0 and abs_pct is None: continue # Invalid transcript

                if ko_pct is None: ko_pct = 0.0
                if st_pct is None: st_pct = 0.0
                if bl_pct is None: bl_pct = 0.0

                prev = self.state.get(abs_id, {})

                config = {
                    'ABS': {
                        'current': abs_pct,
                        'previous': prev.get('abs_pct', 0),
                        'delta': abs(abs_ts - prev.get('abs_ts', 0)) if abs_ts and prev.get('abs_ts', 0) else abs(abs_ts - prev.get('abs_ts', 0)),
                        'threshold': self.delta_abs_thresh,
                        'is_configured': True,
                        'display': ("ABS", "{prev:.4%} -> {curr:.4%}"),
                        'value_formatter': lambda v: f"{v:.4%}"
                    },
                    'KOSYNC': {
                        'current': ko_pct,
                        'previous': prev.get('kosync_pct', 0),
                        'delta': abs(ko_pct - prev.get('kosync_pct', 0)),
                        'threshold': self.delta_kosync_thresh,
                        'is_configured': self.kosync_client.is_configured(),
                        'display': ("KoSync", "{prev:.4%} -> {curr:.4%}"),
                        'value_formatter': lambda v: f"{v*100:.4f}%"
                    },
                    'STORYTELLER': {
                        'current': st_pct,
                        'previous': prev.get('storyteller_pct', 0),
                        'delta': abs(st_pct - prev.get('storyteller_pct', 0)),
                        'threshold': self.delta_kosync_thresh,
                        'is_configured': self.storyteller_db.is_configured(),
                        'display': ("Storyteller", "{prev:.4%} -> {curr:.4%}"),
                        'value_formatter': lambda v: f"{v*100:.4f}%"
                    },
                    'BOOKLORE': {
                        'current': bl_pct,
                        'previous': prev.get('booklore_pct', 0),
                        'delta': abs(bl_pct - prev.get('booklore_pct', 0)),
                        'threshold': self.delta_kosync_thresh,
                        'is_configured': self.booklore_client.is_configured(),
                        'display': ("BookLore", "{prev:.4%} -> {curr:.4%}"),
                        'value_formatter': lambda v: f"{v*100:.4f}%"
                    }
                }

                # Filter config to only include configured services
                filtered_config = {k: v for k, v in config.items() if v.get('is_configured', True)}

                # Check if all 'delta' fields in filtered_config are zero, if so, skip processing
                if all(cfg['delta'] == 0 for cfg in filtered_config.values()):
                    if abs_id not in self.state:
                        self.state[abs_id] = prev
                    self.state[abs_id]['last_updated'] = prev.get('last_updated', 0)
                    continue

                # Small changes (below thresholds) should be noisy-reduced
                small_changes = []
                for key, cfg in filtered_config.items():
                    delta = cfg['delta']
                    threshold = cfg['threshold']
                    if 0 < delta < threshold:
                        label, fmt = cfg['display']
                        delta_str = cfg['value_formatter'](delta)
                        small_changes.append(f"✋ {label} delta {delta_str} (Below threshold): {title_snip}")

                if small_changes and not any(cfg['delta'] >= cfg['threshold'] for cfg in filtered_config.values()):
                    for s in small_changes:
                        logger.info(s)
                    # No further action for only-small changes
                    continue

                # At this point we have a significant change to act on
                logger.info(f"🔄 Change detected for '{title_snip}'")

                # Status block - show only changed lines
                status_lines = []
                for key, cfg in filtered_config.items():
                    if cfg['delta'] > 0:
                        prev = cfg['previous']
                        curr = cfg['current']
                        label, fmt = cfg['display']
                        status_lines.append(f"📊 {label}: {fmt.format(prev=prev, curr=curr)}")

                for line in status_lines:
                    logger.info(line)

                # Build vals from filtered_config
                vals = {k: v['current'] for k, v in filtered_config.items()}

                leader = max(vals, key=vals.get)
                leader_formatter = filtered_config[leader]['value_formatter']
                logger.info(f"📖 [{title_snip}] {leader} leads at {leader_formatter(vals[leader])}")

                final_ts, final_pct = abs_ts, vals[leader]
                sync_success = False

                # --- LEADER LOGIC (unchanged behaviour, but with post-call success logs) ---
                if leader == 'ABS':
                    txt = self.transcriber.get_text_at_time(mapping.get('transcript_file'), abs_ts)
                    if txt:
                        match_pct, rich_locator = self.ebook_parser.find_text_location(epub, txt, hint_percentage=abs_pct)
                        if match_pct:
                            kosync_xpath = rich_locator["xpath"] if rich_locator and rich_locator.get("xpath") else None
                            kosync_ok = self.kosync_client.update_progress(ko_id, match_pct, kosync_xpath)
                            st_ok = self.storyteller_db.update_progress(epub, match_pct, rich_locator)
                            bl_ok = self.booklore_client.update_progress(epub, match_pct, rich_locator)
                            if kosync_ok: logger.info("✅ KoSync update successful")
                            if st_ok: logger.info("✅ Storyteller update successful")
                            if bl_ok: logger.info("✅ Booklore update successful")
                            final_pct = match_pct
                            sync_success = True

                elif leader == 'KOSYNC':
                    txt = None
                    if ko_xpath:
                        logger.debug(f"📚 [{title_snip}] Attempting XPath resolution: {ko_xpath}")
                        txt = self.ebook_parser.resolve_xpath(epub, ko_xpath)
                        if txt:
                            logger.debug(f"   📝 Using XPath text from {ko_xpath}")
                        else:
                            logger.warning(f"⚠️ [{title_snip}] XPath resolution failed for: {ko_xpath}")
                    else:
                        logger.debug(f"⚠️ [{title_snip}] No XPath available from KoSync - using percentage fallback")

                    if not txt:
                        txt = self.ebook_parser.get_text_at_percentage(epub, ko_pct)
                        logger.debug(f"   📝 Using ebook text at {ko_pct:.1%} (fallback)")

                    if txt:
                        ts = self.transcriber.find_time_for_text(mapping.get('transcript_file'), txt, hint_percentage=ko_pct)
                        if ts:
                            abs_ok, final_ts = self._update_abs_progress_with_offset(abs_id, ts)
                            match_pct, rich_locator = self.ebook_parser.find_text_location(epub, txt, hint_percentage=ko_pct)
                            if match_pct:
                                st_ok = self.storyteller_db.update_progress(epub, match_pct, rich_locator)
                                bl_ok = self.booklore_client.update_progress(epub, match_pct, rich_locator)
                                if st_ok: logger.info("✅ Storyteller update successful")
                                if bl_ok: logger.info("✅ Booklore update successful")
                                final_pct = match_pct
                            else:
                                st_ok = self.storyteller_db.update_progress(epub, ko_pct, None)
                                bl_ok = self.booklore_client.update_progress(epub, ko_pct, None)
                                if st_ok: logger.info("✅ Storyteller update successful")
                                if bl_ok: logger.info("✅ Booklore update successful")
                                final_pct = ko_pct
                            sync_success = True

                elif leader == 'STORYTELLER':
                    txt = self.get_text_from_storyteller_fragment(epub, st_href, st_frag) if st_frag else None
                    if not txt: txt = self.ebook_parser.get_text_at_percentage(epub, st_pct)

                    if txt:
                        ts = self.transcriber.find_time_for_text(mapping.get('transcript_file'), txt, hint_percentage=st_pct)
                        if ts:
                            abs_ok, final_ts = self._update_abs_progress_with_offset(abs_id, ts)
                            match_pct, rich_locator = self.ebook_parser.find_text_location(epub, txt, hint_percentage=st_pct)
                            kosync_xpath = rich_locator["xpath"] if rich_locator and rich_locator.get("xpath") else None
                            if match_pct:
                                ko_ok = self.kosync_client.update_progress(ko_id, match_pct, kosync_xpath)
                                bl_ok = self.booklore_client.update_progress(epub, match_pct, rich_locator)
                                if ko_ok: logger.info("✅ KoSync update successful")
                                if bl_ok: logger.info("✅ Booklore update successful")
                                final_pct = match_pct
                            else:
                                ko_ok = self.kosync_client.update_progress(ko_id, st_pct, kosync_xpath)
                                bl_ok = self.booklore_client.update_progress(epub, st_pct, None)
                                if ko_ok: logger.info("✅ KoSync update successful")
                                if bl_ok: logger.info("✅ Booklore update successful")
                                final_pct = st_pct
                            sync_success = True

                elif leader == 'BOOKLORE':
                    txt = self.ebook_parser.get_text_at_percentage(epub, bl_pct)
                    if txt:
                        ts = self.transcriber.find_time_for_text(mapping.get('transcript_file'), txt, hint_percentage=bl_pct)
                        if ts:
                            abs_ok, final_ts = self._update_abs_progress_with_offset(abs_id, ts)
                            match_pct, rich_locator = self.ebook_parser.find_text_location(epub, txt, hint_percentage=bl_pct)
                            kosync_xpath = rich_locator["xpath"] if rich_locator and rich_locator.get("xpath") else None
                            if match_pct:
                                ko_ok = self.kosync_client.update_progress(ko_id, match_pct, kosync_xpath)
                                st_ok = self.storyteller_db.update_progress(epub, match_pct, rich_locator)
                                if ko_ok: logger.info("✅ KoSync update successful")
                                if st_ok: logger.info("✅ Storyteller update successful")
                                final_pct = match_pct
                            else:
                                ko_ok = self.kosync_client.update_progress(ko_id, bl_pct, kosync_xpath)
                                st_ok = self.storyteller_db.update_progress(epub, bl_pct, None)
                                if ko_ok: logger.info("✅ KoSync update successful")
                                if st_ok: logger.info("✅ Storyteller update successful")
                                final_pct = bl_pct
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
                logger.info("💾 State saved to last_state.json")

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
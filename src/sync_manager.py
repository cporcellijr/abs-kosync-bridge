# [START FILE: abs-kosync-enhanced/main.py]
import json
import logging
import os
import threading
import time
import traceback
from pathlib import Path

import schedule

from src.sync_clients.sync_client_interface import UpdateProgressRequest, LocatorResult, ServiceState, SyncResult, SyncClient
# Logging utilities (placed at top to ensure availability during sync)
from src.utils.logging_utils import sanitize_log_data

# Silence noisy third-party loggers
for noisy in ('urllib3', 'requests', 'schedule', 'chardet', 'multipart', 'faster_whisper'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# Only call basicConfig if logging hasn't been configured already (by memory_logger)
root_logger = logging.getLogger()
if not hasattr(root_logger, '_configured') or not root_logger._configured:
    logging.basicConfig(
        level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
        format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    )
logger = logging.getLogger(__name__)


class SyncManager:
    def __init__(self,
                 abs_client=None,
                 kosync_client=None,
                 hardcover_client=None,
                 storyteller_db=None,
                 booklore_client=None,
                 transcriber=None,
                 ebook_parser=None,
                 db_handler=None,
                 state_handler=None,
                 sync_clients: dict[str, SyncClient]=None,
                 kosync_use_percentage_from_server=None,
                 epub_cache_dir=None,
                 data_dir=None,
                 books_dir=None):

        logger.info("=== Sync Manager Starting (Release 6.0 - Precision XPath with DI) ===")
        # Use dependency injection
        self.abs_client = abs_client
        self.kosync_client = kosync_client
        self.hardcover_client = hardcover_client
        self.storyteller_db = storyteller_db
        self.booklore_client = booklore_client
        self._transcriber = transcriber
        self.ebook_parser = ebook_parser
        self.db_handler = db_handler
        self.state_handler = state_handler
        self.data_dir = data_dir
        self.books_dir = books_dir

        self.kosync_use_percentage_from_server = kosync_use_percentage_from_server if kosync_use_percentage_from_server is not None else os.getenv("KOSYNC_USE_PERCENTAGE_FROM_SERVER", "false").lower() == "true"
        self.sync_delta_between_clients = float(os.getenv("SYNC_DELTA_BETWEEN_CLIENTS_PERCENT", 1)) / 100.0
        self.epub_cache_dir = epub_cache_dir or (self.data_dir / "epub_cache" if self.data_dir else Path("/data/epub_cache"))

        self.db = self.db_handler.load(default={"mappings": []})
        self.state = self.state_handler.load(default={})

        self._job_queue = []
        self._job_lock = threading.Lock()
        self._job_thread = None

        self.startup_checks()
        self.cleanup_stale_jobs()
        self._setup_sync_clients(sync_clients)

    def _setup_sync_clients(self, clients: dict[str, SyncClient]):
        self.sync_clients = {name: client for name, client in clients.items() if client.is_configured()}

    @property
    def transcriber(self):
        if self._transcriber is None:
            from src.utils.transcriber import AudioTranscriber
            self._transcriber = AudioTranscriber(self.data_dir or Path("/data"))
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
        if not self.hardcover_client.is_configured(): return
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
            mapping.update(
                {'hardcover_book_id': match['book_id'], 'hardcover_edition_id': match.get('edition_id'), 'hardcover_pages': match.get('pages')})
            self.db_handler.save(self.db)
            # UPDATED: Set status to 1 (Want to Read) initially
            self.hardcover_client.update_status(int(match.get('book_id')), 1, match.get('edition_id'))
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

    def _abs_to_percentage(self, abs_seconds, transcript_path):
        try:
            with open(transcript_path, 'r') as f:
                data = json.load(f)
                dur = data[-1]['end'] if isinstance(data, list) else data.get('duration', 0)
                return min(max(abs_seconds / dur, 0.0), 1.0) if dur > 0 else None
        except:
            return None

    def _get_local_epub(self, ebook_filename):
        """
        Get local path to EPUB file, downloading from Booklore if necessary.
        """
        # First, try to find on filesystem
        books_search_dir = self.books_dir or Path("/books")
        filesystem_matches = list(books_search_dir.glob(f"**/{ebook_filename}"))
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

        for mapping in self.db.get('mappings', []):
            if mapping.get('status') != 'active': continue
            abs_id, ko_id, epub = mapping['abs_id'], mapping['kosync_doc_id'], mapping['ebook_filename']
            logger.info(f"🔄 Syncing '{sanitize_log_data(mapping.get('abs_title', 'Unknown'))}'")
            title_snip = sanitize_log_data(mapping.get('abs_title', 'Unknown'))

            try:
                prev = self.state.get(abs_id, {})

                # Build config using sync_clients - each client fetches its own state
                config: dict[str, ServiceState] = {}
                for client_name, client in self.sync_clients.items():
                    state = client.get_service_state(mapping, prev, title_snip)
                    if state is not None:
                        config[client_name] = state
                        logger.debug(f"[{title_snip}] {client_name} state: {state.current}")

                # Filtered config now only contains non-None states
                if not config:
                    continue  # No valid states to process

                # Check for ABS offline condition
                abs_state = config.get('ABS')
                if abs_state is None:
                    continue  # ABS offline

                # Check if all 'delta' fields in config are zero, if so, skip processing
                if all(round(cfg.delta, 2) == 0 for cfg in config.values()):
                    if abs_id not in self.state:
                        # no state yet, initialize
                        self.state[abs_id] = {}
                    self.state[abs_id]['last_updated'] = prev.get('last_updated', 0)
                    continue

                # check for sync delta threshold between clients. This is to prevent small differences causing constant hops between who is the leader
                progress_values = [cfg.current.get('pct', 0) for cfg in config.values() if cfg.current.get('pct') is not None]
                if len(progress_values) > 1:
                    max_progress = max(progress_values)
                    min_progress = min(progress_values)
                    progress_diff = max_progress - min_progress
                    if progress_diff < self.sync_delta_between_clients:
                        if abs_id not in self.state:
                            # no state yet, initialize
                            self.state[abs_id] = {}
                        self.state[abs_id]['last_updated'] = prev.get('last_updated', 0)
                        logger.debug(f"[{title_snip}] Progress difference {progress_diff:.2%} below threshold {self.sync_delta_between_clients:.2%} - skipping sync")
                        continue

                # Small changes (below thresholds) should be noisy-reduced
                small_changes = []
                for key, cfg in config.items():
                    delta = cfg.delta
                    threshold = cfg.threshold
                    if 0 < delta < threshold:
                        label, fmt = cfg.display
                        delta_str = cfg.value_seconds_formatter(delta) if cfg.value_seconds_formatter else cfg.value_formatter(delta)
                        small_changes.append(f"✋ [{title_snip}] {label} delta {delta_str} (Below threshold): {title_snip}")

                if small_changes and not any(cfg.delta >= cfg.threshold for cfg in config.values()):
                    for s in small_changes:
                        logger.info(s)
                    # No further action for only-small changes
                    continue

                # At this point we have a significant change to act on
                logger.info(f"🔄 [{title_snip}] Change detected")

                # Status block - show only changed lines
                status_lines = []
                for key, cfg in config.items():
                    if cfg.delta > 0:
                        prev = cfg.previous_pct
                        curr = cfg.current.get('pct')
                        label, fmt = cfg.display
                        status_lines.append(f"📊 {label}: {fmt.format(prev=prev, curr=curr)}")

                for line in status_lines:
                    logger.info(line)

                # Build vals from config
                vals = {k: v.current.get('pct') for k, v in config.items()}

                leader = max(vals, key=vals.get)
                leader_formatter = config[leader].value_formatter
                leader_pct = vals[leader]
                logger.info(f"📖 [{title_snip}] {leader} leads at {leader_formatter(leader_pct)}")

                leader_client = self.sync_clients[leader]
                leader_state = config[leader]

                # Get canonical text from leader
                txt = leader_client.get_text_from_current_state(mapping, leader_state)
                if not txt:
                    logger.warning(f"⚠️ [{title_snip}] Could not get text from leader {leader}")
                    continue

                # Get locator (percentage, xpath, etc) from text
                locator = leader_client.get_locator_from_text(txt, epub, leader_pct)
                if not locator:
                    logger.warning(f"⚠️ [{title_snip}] Could not resolve locator from text for leader {leader}, falling back to percentage of leader.")
                    locator = LocatorResult(percentage=leader_pct)

                # Update all other clients and store results
                results: dict[str, SyncResult] = {}
                for client_name, client in self.sync_clients.items():
                    if client_name == leader or client_name not in config:
                        continue
                    request = UpdateProgressRequest(locator, txt, previous_location=config[client_name].previous_pct)
                    result = client.update_progress(mapping, request)
                    results[client_name] = result
                    if result.success:
                        logger.info(f"✅ [{title_snip}] {client_name} update successful to {result.location}")

                # Set final_pct based on results (use match_pct or aggregate if needed)
                final_pct = locator.percentage

                if final_pct > 0.01:
                    if not mapping.get('hardcover_book_id'): self._automatch_hardcover(mapping)
                    if mapping.get('hardcover_book_id'): self._sync_to_hardcover(mapping, final_pct)

                # Build state from sync results and leader state
                new_state = {'last_updated': time.time()}

                # Add leader state with client name prefix
                leader_state_data = leader_state.current
                for key, value in leader_state_data.items():
                    new_state[f"{leader.lower()}_{key}"] = value

                # Add sync results from other clients with client name prefix
                for client_name, result in results.items():
                    if result.success:
                        # Use updated_state if provided, otherwise fall back to basic state
                        state_data = result.updated_state if result.updated_state else {'pct': result.location}
                        for key, value in state_data.items():
                            new_state[f"{client_name.lower()}_{key}"] = value

                self.state[abs_id] = new_state
                self.state_handler.save(self.state)
                logger.info(f"💾 [{title_snip}] State saved to last_state.json")

            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"Sync error: {e}", e)
        if db_dirty: self.db_handler.save(self.db)

    def run_daemon(self):
        """Legacy method - daemon is now run from web_server.py"""
        logger.warning("run_daemon() called - daemon should be started from web_server.py instead")
        schedule.every(int(os.getenv("SYNC_PERIOD_MINS", 5))).minutes.do(self.sync_cycle)
        schedule.every(1).minutes.do(self.check_pending_jobs)
        logger.info("Daemon started.")
        self.sync_cycle()
        while True:
            schedule.run_pending()
            time.sleep(30)

if __name__ == "__main__":
    # This is only used for standalone testing - production uses web_server.py
    logger.info("🚀 Running sync manager in standalone mode (for testing)")

    from src.utils.di_container import create_container
    di_container = create_container()
    # Try to use dependency injection, fall back to legacy if there are issues
    sync_manager = di_container.sync_manager()
    logger.info("✅ Using dependency injection")

    sync_manager.run_daemon()
# [END FILE]

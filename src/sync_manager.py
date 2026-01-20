# [START FILE: abs-kosync-enhanced/main.py]
import logging
import os
import threading
import time
import traceback
from pathlib import Path
import schedule

from src.api.storyteller_api import StorytellerDBWithAPI
from src.db.models import Job
from src.db.models import State, Book
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
                 booklore_client=None,
                 hardcover_client=None,
                 transcriber=None,
                 ebook_parser=None,
                 database_service=None,
                 storyteller_client: StorytellerDBWithAPI=None,
                 sync_clients: dict[str, SyncClient]=None,
                 epub_cache_dir=None,
                 data_dir=None,
                 books_dir=None):

        logger.info("=== Sync Manager Starting (Release 6.0 - Precision XPath with DI) ===")
        # Use dependency injection
        self.abs_client = abs_client
        self.booklore_client = booklore_client
        self.hardcover_client = hardcover_client
        self.transcriber = transcriber
        self.ebook_parser = ebook_parser
        self.database_service = database_service
        self.storyteller_client = storyteller_client
        self.data_dir = data_dir
        self.books_dir = books_dir

        self.sync_delta_between_clients = float(os.getenv("SYNC_DELTA_BETWEEN_CLIENTS_PERCENT", 1)) / 100.0
        self.epub_cache_dir = epub_cache_dir or (self.data_dir / "epub_cache" if self.data_dir else Path("/data/epub_cache"))

        self._job_queue = []
        self._job_lock = threading.Lock()
        self._job_thread = None

        self._setup_sync_clients(sync_clients)
        self.startup_checks()
        self.cleanup_stale_jobs()

    def _setup_sync_clients(self, clients: dict[str, SyncClient]):
        self.sync_clients = {name: client for name, client in clients.items() if client.is_configured()}

    def startup_checks(self):
        # Check configured sync clients
        for client_name, client in (self.sync_clients or {}).items():
            try:
                client.check_connection()
                logger.info(f"✅ {client_name} connection verified")
            except Exception as e:
                logger.warning(f"⚠️ {client_name} connection failed: {e}")

    def cleanup_stale_jobs(self):
        """Reset jobs that were interrupted mid-process on restart."""
        try:
            # Get books with crashed status and reset them to active
            crashed_books = self.database_service.get_books_by_status('crashed')
            for book in crashed_books:
                book.status = 'active'
                self.database_service.save_book(book)
                logger.info(f"[JOB] Reset crashed book status: {sanitize_log_data(book.abs_title)}")

            # Get books with processing status and mark them for retry
            processing_books = self.database_service.get_books_by_status('processing')
            for book in processing_books:
                logger.info(f"[JOB] Recovering interrupted job: {sanitize_log_data(book.abs_title)}")
                book.status = 'failed_retry_later'
                self.database_service.save_book(book)

                # Also update the job record with error info
                job = Job(
                    abs_id=book.abs_id,
                    last_attempt=time.time(),
                    retry_count=0,
                    last_error='Interrupted by restart'
                )
                self.database_service.save_job(job)

        except Exception as e:
            logger.error(f"Error cleaning up stale jobs: {e}")

    def get_abs_title(self, ab):
        media = ab.get('media', {})
        metadata = media.get('metadata', {})
        return metadata.get('title') or ab.get('name', 'Unknown')

    def get_duration(self, ab):
        """Extract duration from audiobook media data."""
        media = ab.get('media', {})
        return media.get('duration', 0)

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

        # 2. Find ONE pending book/job to start using database service
        target_book = None
        eligible_books = []
        max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))
        retry_delay_mins = int(os.getenv("JOB_RETRY_DELAY_MINS", 15))

        # Get books with pending status
        pending_books = self.database_service.get_books_by_status('pending')
        for book in pending_books:
            eligible_books.append(book)
            if not target_book:
                target_book = book

        # Get books that failed but are eligible for retry
        if not target_book:
            failed_books = self.database_service.get_books_by_status('failed_retry_later')
            for book in failed_books:
                # Check if this book has a job record and if it's eligible for retry
                job = self.database_service.get_latest_job(book.abs_id)
                if job:
                    retry_count = job.retry_count or 0
                    last_attempt = job.last_attempt or 0

                    # Skip if max retries exceeded
                    if retry_count >= max_retries:
                        continue

                    # Check if enough time has passed since last attempt
                    if time.time() - last_attempt > retry_delay_mins * 60:
                        eligible_books.append(book)
                        if not target_book:
                            target_book = book

        if not target_book:
            return

        total_jobs = len(eligible_books)
        job_idx = (eligible_books.index(target_book) + 1) if total_jobs else 1

        # 3. Mark book as 'processing' and create/update job record
        logger.info(f"[JOB {job_idx}/{total_jobs}] Starting background transcription: {sanitize_log_data(target_book.abs_title)}")

        # Update book status to processing
        target_book.status = 'processing'
        self.database_service.save_book(target_book)

        # Create or update job record
        job = Job(
            abs_id=target_book.abs_id,
            last_attempt=time.time(),
            retry_count=0,  # Will be updated on failure
            last_error=None,
            progress=0.0
        )
        self.database_service.save_job(job)

        # 4. Launch the heavy work in a separate thread
        self._job_thread = threading.Thread(
            target=self._run_background_job,
            args=(target_book, job_idx, total_jobs),
            daemon=True
        )
        self._job_thread.start()

    def _run_background_job(self, book: Book, job_idx=1, job_total=1):
        """
        Threaded worker that handles transcription without blocking the main loop.
        """
        abs_id = book.abs_id
        abs_title = book.abs_title or 'Unknown'
        ebook_filename = book.ebook_filename
        max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))

        # Milestone log for background job
        logger.info(f"[JOB {job_idx}/{job_total}] Processing '{sanitize_log_data(abs_title)}'...")

        try:
            def update_progress(local_pct, phase):
                """
                Map local phase progress to global 0-100% progress.
                Phase 1: 0-10%
                Phase 2: 10-90%
                Phase 3: 90-100%
                """
                global_pct = 0.0
                if phase == 1:
                    global_pct = 0.0 + (local_pct * 0.1)
                elif phase == 2:
                    global_pct = 0.1 + (local_pct * 0.8)
                elif phase == 3:
                    global_pct = 0.9 + (local_pct * 0.1)
                
                # Save to DB every time for now (or throttle if too frequent)
                self.database_service.update_latest_job(abs_id, progress=global_pct)

            # --- Heavy Lifting (Blocks this thread, but not the Main thread) ---
            # Step 1: Get EPUB file
            update_progress(0.0, 1)
            epub_path = self._get_local_epub(ebook_filename)
            update_progress(1.0, 1) # Done with step 1
            if not epub_path:
                raise FileNotFoundError(f"Could not locate or download: {ebook_filename}")

            # Step 2: Try Fast-Path (SMIL Extraction)
            transcript_path = None

            # Fetch item details to get chapters (for time alignment)
            item_details = self.abs_client.get_item_details(abs_id)
            chapters = item_details.get('media', {}).get('chapters', []) if item_details else []

            # Attempt SMIL extraction
            if hasattr(self.transcriber, 'transcribe_from_smil'):
                 transcript_path = self.transcriber.transcribe_from_smil(
                     abs_id, epub_path, chapters, 
                     progress_callback=lambda p: update_progress(p, 2)
                 )

            # Step 3: Fallback to Whisper (Slow Path) - Only runs if SMIL failed
            if not transcript_path:
                logger.info("ℹ️ SMIL data not found or failed, falling back to Whisper transcription.")
                audio_files = self.abs_client.get_audio_files(abs_id)
                transcript_path = self.transcriber.process_audio(
                    abs_id, audio_files, 
                    progress_callback=lambda p: update_progress(p, 2)
                )
            else:
                # If SMIL worked, it's already done with transcribing phase
                update_progress(1.0, 2)

            # Step 4: Parse EPUB
            self.ebook_parser.extract_text_and_map(
                epub_path, 
                progress_callback=lambda p: update_progress(p, 3)
            )

            # --- Success Update using database service ---
            # Update book with transcript path and set to active
            book.transcript_file = str(transcript_path)
            book.status = 'active'
            self.database_service.save_book(book)

            # Update job record to reset retry count and mark 100%
            job = self.database_service.get_latest_job(abs_id)
            if job:
                job.retry_count = 0
                job.last_error = None
                job.progress = 1.0
                self.database_service.save_job(job)


            logger.info(f"[JOB] Completed: {sanitize_log_data(abs_title)}")

        except Exception as e:
            logger.error(f"[FAIL] {sanitize_log_data(abs_title)}: {e}")

            # --- Failure Update using database service ---
            # Get current job to increment retry count
            job = self.database_service.get_latest_job(abs_id)
            current_retry_count = job.retry_count if job else 0
            new_retry_count = current_retry_count + 1

            # Update job record
            from src.db.models import Job
            updated_job = Job(
                abs_id=abs_id,
                last_attempt=time.time(),
                retry_count=new_retry_count,
                last_error=str(e),
                progress=job.progress if job else 0.0
            )
            self.database_service.save_job(updated_job)

            # Update book status based on retry count
            if new_retry_count >= max_retries:
                book.status = 'failed_permanent'
                logger.warning(f"[JOB] {sanitize_log_data(abs_title)}: Max retries exceeded")
            else:
                book.status = 'failed_retry_later'

            self.database_service.save_book(book)

    def sync_cycle(self):
        # Get active books directly from database service
        active_books = self.database_service.get_books_by_status('active')
        if active_books:
            logger.debug(f"🔄 Sync cycle starting - {len(active_books)} active book(s)")

        # Main sync loop - process each active book
        for book in active_books:
            abs_id = book.abs_id
            logger.info(f"🔄 Syncing '{sanitize_log_data(book.abs_title or 'Unknown')}'")
            title_snip = sanitize_log_data(book.abs_title or 'Unknown')

            try:
                # Get previous state for this book from database
                previous_states = self.database_service.get_states_for_book(abs_id)

                # Create a mapping of client names to their previous states
                prev_states_by_client = {}
                last_updated = 0
                for state in previous_states:
                    prev_states_by_client[state.client_name] = state
                    if state.last_updated and state.last_updated > last_updated:
                        last_updated = state.last_updated

                # Build config using sync_clients - each client fetches its own state
                config: dict[str, ServiceState] = {}
                for client_name, client in self.sync_clients.items():
                    # Get the previous state for this specific client
                    prev_state = prev_states_by_client.get(client_name.lower())
                    state = client.get_service_state(book, prev_state, title_snip)
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
                    continue

                # check for sync delta threshold between clients. This is to prevent small differences causing constant hopping
                progress_values = [cfg.current.get('pct', 0) for cfg in config.values() if cfg.current.get('pct') is not None]
                if len(progress_values) > 1:
                    max_progress = max(progress_values)
                    min_progress = min(progress_values)
                    progress_diff = max_progress - min_progress
                    if progress_diff < self.sync_delta_between_clients:
                        logger.debug(f"[{title_snip}] Progress difference {progress_diff:.2%} below threshold {self.sync_delta_between_clients:.2%} - skipping sync")
                        continue

                # Small changes (below thresholds) should be noisy-reduced
                small_changes = []
                for key, cfg in config.items():
                    delta = cfg.delta
                    threshold = cfg.threshold
                    
                    # Debug logging for potential None values
                    if delta is None or threshold is None:
                         logger.debug(f"[{title_snip}] {key} delta={delta}, threshold={threshold}")

                    if delta is not None and threshold is not None and 0 < delta < threshold:
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

                # Build vals from config - only include clients that can be leaders
                vals = {}
                for k, v in config.items():
                    client = self.sync_clients[k]
                    if client.can_be_leader():
                        vals[k] = v.current.get('pct')

                # Ensure we have at least one potential leader
                if not vals:
                    logger.warning(f"⚠️ [{title_snip}] No clients available to be leader")
                    continue

                leader = max(vals, key=vals.get)
                leader_formatter = config[leader].value_formatter
                leader_pct = vals[leader]
                logger.info(f"📖 [{title_snip}] {leader} leads at {leader_formatter(leader_pct)}")

                leader_client = self.sync_clients[leader]
                leader_state = config[leader]

                # Get canonical text from leader
                txt = leader_client.get_text_from_current_state(book, leader_state)
                if not txt:
                    logger.warning(f"⚠️ [{title_snip}] Could not get text from leader {leader}")
                    continue

                # Get locator (percentage, xpath, etc) from text
                epub = book.ebook_filename
                locator = leader_client.get_locator_from_text(txt, epub, leader_pct)
                if not locator:
                    logger.warning(f"⚠️ [{title_snip}] Could not resolve locator from text for leader {leader}, falling back to percentage of leader.")
                    locator = LocatorResult(percentage=leader_pct)

                # Update all other clients and store results
                results: dict[str, SyncResult] = {}
                for client_name, client in self.sync_clients.items():
                    if client_name == leader:
                        continue
                    try:
                        request = UpdateProgressRequest(locator, txt, previous_location=config.get(client_name).previous_pct if config.get(client_name) else None)
                        result = client.update_progress(book, request)
                        results[client_name] = result
                    except Exception as e:
                        logger.error(f"⚠️ Failed to update {client_name}: {e}")
                        results[client_name] = SyncResult(None, False)

                # Save states directly to database service using State models
                current_time = time.time()

                # Save leader state
                leader_state_data = leader_state.current

                leader_state_model = State(
                    abs_id=book.abs_id,
                    client_name=leader.lower(),
                    last_updated=current_time,
                    percentage=leader_state_data.get('pct'),
                    timestamp=leader_state_data.get('ts'),
                    xpath=leader_state_data.get('xpath'),
                    cfi=leader_state_data.get('cfi')
                )
                self.database_service.save_state(leader_state_model)

                # Save sync results from other clients
                for client_name, result in results.items():
                    if result.success:
                        # Use updated_state if provided, otherwise fall back to basic state
                        state_data = result.updated_state if result.updated_state else {'pct': result.location}
                        logger.info(f"[{title_snip}] Updated state data for {client_name}: " + str(state_data))
                        client_state_model = State(
                            abs_id=book.abs_id,
                            client_name=client_name.lower(),
                            last_updated=current_time,
                            percentage=state_data.get('pct'),
                            timestamp=state_data.get('ts'),
                            xpath=state_data.get('xpath'),
                            cfi=state_data.get('cfi')
                        )
                        self.database_service.save_state(client_state_model)

                logger.info(f"💾 [{title_snip}] States saved to database")
                
                # Debugging crash: Flush logs to ensure we see this before any potential hard crash
                for handler in logger.handlers:
                    handler.flush()
                if hasattr(root_logger, 'handlers'):
                    for handler in root_logger.handlers:
                        handler.flush()

            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"Sync error: {e}")
        
        logger.debug(f"End of sync cycle for active books")

    def clear_progress(self, abs_id):
        """
        Clear progress data for a specific book and reset all sync clients to 0%.

        Args:
            abs_id: The book ID to clear progress for

        Returns:
            dict: Summary of cleared data
        """
        try:
            logger.info(f"🧹 Clearing progress for book {sanitize_log_data(abs_id)}...")

            # Get the book first
            book = self.database_service.get_book(abs_id)
            if not book:
                raise ValueError(f"Book not found: {abs_id}")

            # Clear all states for this book from database
            cleared_count = self.database_service.delete_states_for_book(abs_id)
            logger.info(f"📊 Cleared {cleared_count} state records from database")

            # Reset all sync clients to 0% progress
            reset_results = {}
            locator = LocatorResult(percentage=0.0)
            request = UpdateProgressRequest(locator_result=locator, txt="", previous_location=None)

            for client_name, client in self.sync_clients.items():
                try:
                    result = client.update_progress(book, request)
                    reset_results[client_name] = {
                        'success': result.success,
                        'message': 'Reset to 0%' if result.success else 'Failed to reset'
                    }
                    if result.success:
                        logger.info(f"✅ Reset {client_name} to 0%")
                    else:
                        logger.warning(f"⚠️ Failed to reset {client_name}")
                except Exception as e:
                    reset_results[client_name] = {
                        'success': False,
                        'message': str(e)
                    }
                    logger.warning(f"⚠️ Error resetting {client_name}: {e}")

            summary = {
                'book_id': abs_id,
                'book_title': book.abs_title,
                'database_states_cleared': cleared_count,
                'client_reset_results': reset_results,
                'successful_resets': sum(1 for r in reset_results.values() if r['success']),
                'total_clients': len(reset_results)
            }

            logger.info(f"✅ Progress clearing completed for '{sanitize_log_data(book.abs_title)}'")
            logger.info(f"   Database states cleared: {cleared_count}")
            logger.info(f"   Client resets: {summary['successful_resets']}/{summary['total_clients']} successful")

            return summary

        except Exception as e:
            error_msg = f"Error clearing progress for {abs_id}: {e}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            raise RuntimeError(error_msg) from e

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

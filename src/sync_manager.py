# [START FILE: abs-kosync-enhanced/main.py]
import glob
import logging
import os
import threading
import time
import traceback
from pathlib import Path
import schedule
from concurrent.futures import ThreadPoolExecutor, as_completed

import json
from src.api.storyteller_api import StorytellerDBWithAPI
from src.db.models import Job
from src.db.models import State, Book, PendingSuggestion
from src.sync_clients.sync_client_interface import UpdateProgressRequest, LocatorResult, ServiceState, SyncResult, SyncClient
# Logging utilities (placed at top to ensure availability during sync)
from src.utils.logging_utils import sanitize_log_data

# [NEW] Service Imports
from src.services.alignment_service import AlignmentService
from src.services.library_service import LibraryService
from src.services.migration_service import MigrationService

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
                 alignment_service: AlignmentService = None,
                 library_service: LibraryService = None,
                 migration_service: MigrationService = None,
                 epub_cache_dir=None,
                 data_dir=None,
                 books_dir=None):

        logger.info("=== Sync Manager Starting ===")
        # Use dependency injection
        self.abs_client = abs_client
        self.booklore_client = booklore_client
        self.hardcover_client = hardcover_client
        self.transcriber = transcriber
        self.ebook_parser = ebook_parser
        self.database_service = database_service
        self.storyteller_client = storyteller_client
        
        # [NEW] Services
        self.alignment_service = alignment_service
        self.library_service = library_service
        self.migration_service = migration_service
        
        self.data_dir = data_dir
        self.books_dir = books_dir

        try:
            val = float(os.getenv("SYNC_DELTA_BETWEEN_CLIENTS_PERCENT", 1))
        except (ValueError, TypeError):
            logger.warning("Invalid SYNC_DELTA_BETWEEN_CLIENTS_PERCENT value, defaulting to 1")
            val = 1.0
        self.sync_delta_between_clients = val / 100.0
        self.delta_chars_thresh = 2000  # ~400 words
        self.epub_cache_dir = epub_cache_dir or (self.data_dir / "epub_cache" if self.data_dir else Path("/data/epub_cache"))

        self._job_queue = []
        self._job_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._job_thread = None

        self._setup_sync_clients(sync_clients)
        self.startup_checks()
        self.cleanup_stale_jobs()
        # Scan for corrupted transcripts check removed


    def _setup_sync_clients(self, clients: dict[str, SyncClient]):
        self.sync_clients = {}
        for name, client in clients.items():
            if client.is_configured():
                self.sync_clients[name] = client
                logger.info(f"[INIT] Sync client enabled: {name}")
            else:
                logger.info(f"[SKIP] Sync client disabled/unconfigured: {name}")

    def startup_checks(self):
        # Check configured sync clients
        for client_name, client in (self.sync_clients or {}).items():
            try:
                client.check_connection()
                logger.info(f"[OK] {client_name} connection verified")
            except Exception as e:
                logger.warning(f"[WARN] {client_name} connection failed: {e}")
        
        # [NEW] Run one-time migration
        if self.migration_service:
            logger.info("🔄 Checking for legacy data to migrate...")
            self.migration_service.migrate_legacy_data()

        # [NEW] Cleanup orphaned cache files
        self.cleanup_cache()

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
            # Get books with processing status OR failed_retry_later and check if they actually finished
            # This covers cases where a job finished but status failed to update, or previous restart marked it failed
            candidates = self.database_service.get_books_by_status('processing') + \
                         self.database_service.get_books_by_status('failed_retry_later')
            
            for book in candidates:
                # Check if alignment actually exists (job finished but status update failed)
                has_alignment = False
                if self.alignment_service:
                    has_alignment = bool(self.alignment_service._get_alignment(book.abs_id))
                
                if has_alignment:
                    # Only log if we are CHANGING status (active is goal)
                    if book.status != 'active':
                        logger.info(f"[JOB] Found orphan alignment for {book.status} book: {sanitize_log_data(book.abs_title)}. Marking ACTIVE.")
                        book.status = 'active'
                        self.database_service.save_book(book)
                elif book.status == 'processing':
                     # Only mark processing checks as failed (failed are already failed)
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

    def cleanup_cache(self):
        """Delete files from ebook cache that are not referenced in the DB."""
        if not self.epub_cache_dir.exists():
            return

        logger.info("🧹 Starting ebook cache cleanup...")
        
        try:
            # 1. Collect all valid filenames from DB
            valid_filenames = set()
            
            # From Active Books
            books = self.database_service.get_all_books()
            for book in books:
                if book.ebook_filename:
                    valid_filenames.add(book.ebook_filename)
            
            # From Pending Suggestions (covers auto-discovery matches)
            suggestions = self.database_service.get_all_pending_suggestions()
            for suggestion in suggestions:
                # matches property automatically parses the JSON
                for match in suggestion.matches:
                    if match.get('filename'):
                        valid_filenames.add(match['filename'])

            # 2. Iterate cache and delete orphans
            deleted_count = 0
            reclaimed_bytes = 0
            
            for file_path in self.epub_cache_dir.iterdir():
                # Only check files, and ensure we don't delete if it's in our valid list
                if file_path.is_file() and file_path.name not in valid_filenames:
                    try:
                        size = file_path.stat().st_size
                        file_path.unlink()
                        deleted_count += 1
                        reclaimed_bytes += size
                        logger.debug(f"   🗑️ Deleted orphaned cache file: {file_path.name}")
                    except Exception as e:
                        logger.warning(f"   ⚠️ Failed to delete {file_path.name}: {e}")
            
            if deleted_count > 0:
                mb = reclaimed_bytes / (1024 * 1024)
                logger.info(f"✨ Cache cleanup complete: Removed {deleted_count} files ({mb:.2f} MB)")
            else:
                logger.info("✨ Cache is clean (no orphaned files found).")
                
        except Exception as e:
            logger.error(f"Error during cache cleanup: {e}")

    def get_abs_title(self, ab):
        media = ab.get('media', {})
        metadata = media.get('metadata', {})
        return metadata.get('title') or ab.get('name', 'Unknown')

    def get_duration(self, ab):
        """Extract duration from audiobook media data."""
        media = ab.get('media', {})
        return media.get('duration', 0)

    def _normalize_for_cross_format_comparison(self, book, config):
        """
        Normalize positions for cross-format comparison (audiobook vs ebook).
        
        When syncing between audiobook (ABS) and ebook clients (KoSync, etc.),
        raw percentages are not comparable because:
        - Audiobook % = time position / total duration
        - Ebook % = text position / total text
        
        These don't correlate linearly. This method converts ebook positions
        to equivalent audiobook timestamps using text-matching, enabling
        accurate comparison of "who is further in the story".
        
        Returns:
            dict: {client_name: normalized_timestamp} for comparison,
                  or None if normalization not possible/needed
        """
        # Check if we have both ABS and ebook clients in the mix
        has_abs = 'ABS' in config
        ebook_clients = [k for k in config.keys() if k != 'ABS']
        
        if not has_abs or not ebook_clients:
            # Same-format sync, raw percentages are fine
            return None
            
        if not book.transcript_file:
            logger.debug(f"[{book.abs_id}] No transcript available for cross-format normalization")
            return None
            
        normalized = {}
        
        # ABS already has timestamp
        abs_state = config['ABS']
        abs_ts = abs_state.current.get('ts', 0)
        normalized['ABS'] = abs_ts
        
        # For each ebook client, get their text and find equivalent timestamp
        for client_name in ebook_clients:
            client = self.sync_clients.get(client_name)
            if not client:
                continue
                
            client_state = config[client_name]
            client_pct = client_state.current.get('pct', 0)
            
            try:
                # [NEW] Get character offset from the ebook position for precise alignment
                book_path = self.ebook_parser.resolve_book_path(book.ebook_filename)
                full_text, _ = self.ebook_parser.extract_text_and_map(book_path)
                total_text_len = len(full_text)
                
                char_offset = int(client_pct * total_text_len)
                txt = full_text[max(0, char_offset - 400):min(total_text_len, char_offset + 400)]
                
                if not txt:
                    logger.debug(f"[{book.abs_id}] Could not get text from {client_name} for normalization")
                    continue
                    
                # Find equivalent timestamp in audiobook using the precise aligner if available
                if self.alignment_service:
                    ts_for_text = self.alignment_service.get_time_for_text(
                        book.abs_id, txt, 
                        char_offset_hint=char_offset
                    )
                else:
                    # Fallback or strict error? 
                    ts_for_text = None
                
                if ts_for_text is not None:
                    normalized[client_name] = ts_for_text
                    logger.debug(f"[{book.abs_id}] Normalized {client_name} {client_pct:.2%} -> {ts_for_text:.1f}s")
                else:
                    logger.debug(f"[{book.abs_id}] Could not find timestamp for {client_name} text")
            except Exception as e:
                logger.warning(f"[{book.abs_id}] Cross-format normalization failed for {client_name}: {e}")
                
        # Only return if we successfully normalized at least one ebook client
        if len(normalized) > 1:
            return normalized
        return None


    def _fetch_states_parallel(self, book, prev_states_by_client, title_snip, bulk_states_per_client=None, clients_to_use=None):
        """Fetch states from specified clients (or all if not specified) in parallel."""
        clients_to_use = clients_to_use or self.sync_clients
        config = {}
        bulk_states_per_client = bulk_states_per_client or {}

        with ThreadPoolExecutor(max_workers=len(clients_to_use)) as executor:
            futures = {}
            for client_name, client in clients_to_use.items():
                prev_state = prev_states_by_client.get(client_name.lower())

                # Get bulk context from the unified dict
                bulk_ctx = bulk_states_per_client.get(client_name)

                future = executor.submit(
                    client.get_service_state, book, prev_state, title_snip, bulk_ctx
                )
                futures[future] = client_name

            for future in as_completed(futures, timeout=15):
                client_name = futures[future]
                try:
                    state = future.result()
                    if state is not None:
                        config[client_name] = state
                except Exception as e:
                    logger.warning(f"⚠️ {client_name} state fetch failed: {e}")

        return config





    def _get_local_epub(self, ebook_filename):
        """
        Get local path to EPUB file, downloading from Booklore if necessary.
        """
        # First, try to find on filesystem
        books_search_dir = self.books_dir or Path("/books")
        escaped_filename = glob.escape(ebook_filename)
        filesystem_matches = list(books_search_dir.glob(f"**/{escaped_filename}"))
        if filesystem_matches:
            logger.info(f"[FS] Found EPUB on filesystem: {filesystem_matches[0]}")
            return filesystem_matches[0]
        
        # Check persistent EPUB cache
        self.epub_cache_dir.mkdir(parents=True, exist_ok=True)
        cached_path = self.epub_cache_dir / ebook_filename
        if cached_path.exists():
            logger.info(f"[CACHE] Found EPUB in cache: {cached_path}")
            return cached_path

        # Try to download from Booklore API
        # Note: We use hasattr to prevent crashes if BookloreClient wasn't updated with these methods yet
        if hasattr(self.booklore_client, 'is_configured') and self.booklore_client.is_configured():
            book = self.booklore_client.find_book_by_filename(ebook_filename)
            if book:
                logger.info(f"[DL] Downloading EPUB from Booklore: {sanitize_log_data(ebook_filename)}")
                if hasattr(self.booklore_client, 'download_book'):
                    content = self.booklore_client.download_book(book['id'])
                    if content:
                        with open(cached_path, 'wb') as f:
                            f.write(content)
                        logger.info(f"[OK] Downloaded EPUB to cache: {cached_path}")
                        return cached_path
                    else:
                        logger.error(f"Failed to download EPUB content from Booklore")
            else:
                logger.error(f"EPUB not found in Booklore: {sanitize_log_data(ebook_filename)}")
            if not filesystem_matches:
                logger.error(f"EPUB not found on filesystem and Booklore not configured")

        return None

    # Suggestion Logic
    def check_for_suggestions(self, abs_progress_map, active_books):
        """Check for unmapped books with progress and create suggestions."""
        suggestions_enabled_val = os.environ.get("SUGGESTIONS_ENABLED", "true")
        logger.debug(f"DEBUG: SUGGESTIONS_ENABLED env var is: '{suggestions_enabled_val}'")
        
        if suggestions_enabled_val.lower() != "true":
            return

        try:
            # optimization: get all mapped IDs to avoid suggesting existing books (even if inactive)
            all_books = self.database_service.get_all_books()
            mapped_ids = {b.abs_id for b in all_books}
            
            logger.debug(f"Checking for suggestions: {len(abs_progress_map)} books with progress, {len(mapped_ids)} already mapped")

            for abs_id, item_data in abs_progress_map.items():
                if abs_id in mapped_ids:
                    logger.debug(f"Skipping {abs_id}: already mapped")
                    continue

                duration = item_data.get('duration', 0)
                current_time = item_data.get('currentTime', 0)
                
                if duration > 0:
                    pct = current_time / duration
                    if pct > 0.01:
                        # Check if a suggestion already exists (pending, dismissed, or ignored)
                        if self.database_service.suggestion_exists(abs_id):
                            logger.debug(f"Skipping {abs_id}: suggestion already exists/dismissed")
                            continue
                            
                        # Check if book is already mostly finished (>70%)
                        # If a user has listened to >70% elsewhere, they probably don't need a suggestion
                        if pct > 0.70:
                             logger.debug(f"Skipping {abs_id}: progress {pct:.1%} > 70% threshold")
                             continue
                        
                        logger.debug(f"Creating suggestion for {abs_id} (progress: {pct:.1%})")    
                        self._create_suggestion(abs_id, item_data)
                    else:
                        logger.debug(f"Skipping {abs_id}: progress {pct:.1%} below 1% threshold")
                else:
                    logger.debug(f"Skipping {abs_id}: no duration")
        except Exception as e:
            logger.error(f"Error checking suggestions: {e}")

    def _create_suggestion(self, abs_id, progress_data):
        """Create a new suggestion for an unmapped book."""
        logger.info(f"[IDEA] Found potential new book for suggestion: {abs_id}")
        
        try:
            # 1. Get Details from ABS
            item = self.abs_client.get_item_details(abs_id)
            if not item:
                logger.debug(f"Suggestion failed: Could not get details for {abs_id}")
                return

            media = item.get('media', {})
            metadata = media.get('metadata', {})
            title = metadata.get('title')
            author = metadata.get('authorName')
            # Use local proxy for cover image to ensure accessibility
            cover = f"/api/cover-proxy/{abs_id}"
            
            logger.debug(f"Checking suggestions for '{title}' (Author: {author})")
            
            matches = []
            
            found_filenames = set()
            
            # 2a. Search Booklore
            if self.booklore_client and self.booklore_client.is_configured():
                try:
                    bl_results = self.booklore_client.search_books(title)
                    logger.debug(f"Booklore returned {len(bl_results)} results for '{title}'")
                    for b in bl_results:
                         # Filter for EPUBs
                         fname = b.get('fileName', '')
                         if fname.lower().endswith('.epub'):
                             found_filenames.add(fname)
                             matches.append({
                                 "source": "booklore",
                                 "title": b.get('title'),
                                 "author": b.get('authors'),
                                 "filename": fname, # Important for auto-linking
                                 "id": str(b.get('id')),
                                 "confidence": "high" if title.lower() in b.get('title', '').lower() else "medium"
                             })
                except Exception as e:
                    logger.warning(f"Booklore search failed during suggestion: {e}")

            # 2b. Search Local Filesystem
            if self.books_dir and self.books_dir.exists():
                try:
                    clean_title = title.lower()
                    fs_matches = 0
                    for epub in self.books_dir.rglob("*.epub"):
                         if epub.name in found_filenames:
                             continue
                         if clean_title in epub.name.lower():
                             fs_matches += 1
                             matches.append({
                                 "source": "filesystem",
                                 "filename": epub.name,
                                 "path": str(epub),
                                 "confidence": "high"
                             })
                    logger.debug(f"Filesystem found {fs_matches} matches")
                except Exception as e:
                    logger.warning(f"Filesystem search failed during suggestion: {e}")
            
            # 3. Save to DB
            if not matches:
                logger.debug(f"[INFO] No matches found for '{title}', skipping suggestion creation.")
                return

            suggestion = PendingSuggestion(
                source_id=abs_id,
                title=title,
                author=author,
                cover_url=cover,
                matches_json=json.dumps(matches)
            )
            self.database_service.save_pending_suggestion(suggestion)
            match_count = len(matches)
            logger.info(f"[OK] Created suggestion for '{title}' with {match_count} matches")

        except Exception as e:
            logger.error(f"Failed to create suggestion for {abs_id}: {e}")
            logger.debug(traceback.format_exc())

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
            raw_transcript = None
            transcript_source = None

            # Fetch item details to get chapters (for time alignment)
            item_details = self.abs_client.get_item_details(abs_id)
            chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
            
            # [NEW] Pre-fetch book text for validation/alignment
            # We need this for Validating SMIL OR for Aligning Whisper
            book_text, _ = self.ebook_parser.extract_text_and_map(epub_path)

            # Attempt SMIL extraction
            if hasattr(self.transcriber, 'transcribe_from_smil'):
                  raw_transcript = self.transcriber.transcribe_from_smil(
                      abs_id, epub_path, chapters,
                      full_book_text=book_text,
                       progress_callback=lambda p: update_progress(p, 2)
                  )
                  if raw_transcript:
                      transcript_source = "SMIL"

            # Step 3: Fallback to Whisper (Slow Path) - Only runs if SMIL failed
            if not raw_transcript:
                logger.info("[INFO] SMIL extraction skipped/failed, falling back to Whisper transcription.")
                
                audio_files = self.abs_client.get_audio_files(abs_id)
                raw_transcript = self.transcriber.process_audio(
                    abs_id, audio_files,
                    full_book_text=book_text, # Passed for context/alignment inside transcriber if old logic used
                    progress_callback=lambda p: update_progress(p, 2)
                )
                if raw_transcript:
                    transcript_source = "WHISPER"
            else:
                # If SMIL worked, it's already done with transcribing phase
                update_progress(1.0, 2)

            if not raw_transcript:
                raise Exception("Failed to generate transcript from both SMIL and Whisper.")

            # Step 4: Parse EPUB (Already did this for text, but ensure map is ready if needed?) 
            # Actually ebook_parser caches result, so repeating is cheap.
            # But we already have 'book_text'.
            
            # [NEW] Step 5: Align and Store using AlignmentService
            # This is where we commit the result to the DB
            logger.info(f"🧠 Aligning transcript ({transcript_source}) using Anchored Alignment...")
            
            # Update progress to show we are working on alignment (Start of Phase 3 = 90%)
            update_progress(0.1, 3) # 91%
            
            success = self.alignment_service.align_and_store(
                abs_id, raw_transcript, book_text, chapters
            )
            
            # Alignment done
            update_progress(0.5, 3) # 95%
            
            if not success:
                raise Exception("Alignment failed to generate valid map.")


            # Step 4: Parse EPUB
            self.ebook_parser.extract_text_and_map(
                epub_path,
                progress_callback=lambda p: update_progress(p, 3)
            )

            # --- Success Update using database service ---
            # Update book with transcript path (Now just a marker or None, as data is in book_alignments)
            book.transcript_file = "DB_MANAGED"
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
                
                # Clean up audio cache on permanent failure to free disk space
                if self.data_dir:
                    import shutil
                    audio_cache_dir = Path(self.data_dir) / "audio_cache" / abs_id
                    if audio_cache_dir.exists():
                        try:
                            shutil.rmtree(audio_cache_dir)
                            logger.info(f"[JOB] Cleaned up audio cache for {sanitize_log_data(abs_title)}")
                        except Exception as cleanup_err:
                            logger.warning(f"[JOB] Failed to clean audio cache: {cleanup_err}")
            else:
                book.status = 'failed_retry_later'

            self.database_service.save_book(book)

    def _has_significant_delta(self, client_name, config, book):
        """
        Check if a client has a significant delta using hybrid time/percentage logic.
        
        Returns True if:
        - Percentage delta > 0.05% (catches large jumps)
        - OR absolute time delta > 30 seconds (catches small but real progress)
        
        This prevents:
        - API noise on short books (0.3s changes don't count)
        - API noise on long books (BookLore's 20s rounding errors filtered)
        - Missing real progress on all books (30s+ changes do count)
        """
        delta_pct = config[client_name].delta
        
        # Quick check: percentage threshold
        MIN_PCT_THRESHOLD = 0.0005  # 0.05%
        if delta_pct > MIN_PCT_THRESHOLD:
            return True
        
        # Time-based check (if we have duration info)
        if hasattr(book, 'duration') and book.duration:
            delta_seconds = delta_pct * book.duration
            MIN_TIME_THRESHOLD = 30  # seconds
            if delta_seconds > MIN_TIME_THRESHOLD:
                return True
                
        return False

    def _determine_leader(self, config, book, abs_id, title_snip):
        """
        Determines which client should be the leader based on:
        1. Most recent change (delta > threshold)
        2. Furthest progress (fallback)
        3. Cross-format normalization (if needed)
        
        Returns:
            tuple: (leader_client_name, leader_percentage) or (None, None)
        """
        # Build vals from config - only include clients that can be leaders
        vals = {}
        for k, v in config.items():
            client = self.sync_clients[k]
            if client.can_be_leader():
                vals[k] = v.current.get('pct')

        # Ensure we have at least one potential leader
        if not vals:
            logger.warning(f"[WARN] [{abs_id}] [{title_snip}] No clients available to be leader")
            return None, None

        # Check which clients have changed (delta > minimum threshold)
        # "Most recent change wins" - if only one client changed, it becomes the leader
        # Use hybrid time/percentage logic to filter out phantom API noise
        clients_with_delta = {k: v for k, v in vals.items() if self._has_significant_delta(k, config, book)}

        leader = None
        leader_pct = None

        if len(clients_with_delta) == 1:
            # Only one client changed - that client is the leader (most recent change wins)
            leader = list(clients_with_delta.keys())[0]
            leader_pct = vals[leader]
            logger.info(f"[LEADER] [{abs_id}] [{title_snip}] {leader} leads at {config[leader].value_formatter(leader_pct)} (only client with change)")
        else:
            # Multiple clients changed or this is a discrepancy resolution
            # Use "furthest wins" logic among changed clients (or all if none changed)
            candidates = clients_with_delta if clients_with_delta else vals
            
            # For cross-format sync (audiobook vs ebook), use normalized timestamps
            normalized_positions = self._normalize_for_cross_format_comparison(book, config)
            
            if normalized_positions and len(normalized_positions) > 1:
                # Filter normalized positions to only include candidates
                normalized_candidates = {k: v for k, v in normalized_positions.items() if k in candidates}
                if normalized_candidates:
                    leader = max(normalized_candidates, key=normalized_candidates.get)
                    leader_ts = normalized_candidates[leader]
                    leader_pct = vals[leader]
                    logger.info(f"[LEADER] [{abs_id}] [{title_snip}] {leader} leads at {config[leader].value_formatter(leader_pct)} (normalized: {leader_ts:.1f}s)")
                else:
                    # Fallback to percentage-based comparison among candidates
                    leader = max(candidates, key=candidates.get)
                    leader_pct = vals[leader]
                    logger.info(f"[LEADER] [{abs_id}] [{title_snip}] {leader} leads at {config[leader].value_formatter(leader_pct)}")
            else:
                # Same-format sync or normalization failed - use raw percentages
                leader = max(candidates, key=candidates.get)
                leader_pct = vals[leader]
                logger.info(f"[LEADER] [{abs_id}] [{title_snip}] {leader} leads at {config[leader].value_formatter(leader_pct)}")
                
        return leader, leader_pct

    def sync_cycle(self, target_abs_id=None):
        """
        Run a sync cycle.

        Args:
            target_abs_id: If provided, only sync this specific book (Instant Sync trigger).
                           Otherwise, sync all active books using bulk-poll optimization.
        """
        # Prevent race condition: If daemon is running, skip. If Instant Sync, wait.
        acquired = False
        if target_abs_id:
             # Instant Sync: Block and wait for lock (up to 10s)
             acquired = self._sync_lock.acquire(timeout=10)
             if not acquired:
                 logger.warning(f"[WARN] Sync lock timeout for {target_abs_id} - skipping")
                 return
        else:
             # Daemon: Non-blocking attempt
             acquired = self._sync_lock.acquire(blocking=False)
             if not acquired:
                 logger.debug("Sync cycle skipped - another cycle is running")
                 return

        try:
            self._sync_cycle_internal(target_abs_id)
        except Exception as e:
            logger.error(f"Sync cycle internal error: {e}")
            # Log traceback for robust debugging
            logger.error(traceback.format_exc())
        finally:
            self._sync_lock.release()

    def _sync_cycle_internal(self, target_abs_id=None):
        # Clear caches at start of cycle
        storyteller_client = self.sync_clients.get('Storyteller')
        if storyteller_client and hasattr(storyteller_client, 'storyteller_client'):
            if hasattr(storyteller_client.storyteller_client, 'clear_cache'):
                storyteller_client.storyteller_client.clear_cache()
                
        # Refresh Library Metadata (Booklore)
        if self.library_service:
            self.library_service.sync_library_books()
    
        # Get active books directly from database service
        active_books = []
        if target_abs_id:
            logger.info(f"[JOB] Instant Sync triggered for {target_abs_id}")
            book = self.database_service.get_book(target_abs_id)
            if book and book.status == 'active':
                active_books = [book]
        else:
            active_books = self.database_service.get_books_by_status('active')

        if not active_books:
            return

        # Optimization: Pre-fetch bulk data from all clients that support it
        # Only do this if we are in a full cycle (target_abs_id is None)
        bulk_states_per_client = {}

        if not target_abs_id:
            logger.debug(f"🔄 Sync cycle starting - {len(active_books)} active book(s)")
            for client_name, client in self.sync_clients.items():
                bulk_data = client.fetch_bulk_state()
                if bulk_data:
                    bulk_states_per_client[client_name] = bulk_data
                    logger.debug(f"📊 Pre-fetched bulk state for {client_name}")
            
            # Check for suggestions
            if 'ABS' in bulk_states_per_client:
                self.check_for_suggestions(bulk_states_per_client['ABS'], active_books)
                
        # Main sync loop - process each active book
        for book in active_books:
            abs_id = book.abs_id
            logger.info(f"🔄 [{abs_id}] Syncing '{sanitize_log_data(book.abs_title or 'Unknown')}'")
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

                # Determine active clients based on sync_mode using interface method
                sync_type = 'ebook' if (hasattr(book, 'sync_mode') and book.sync_mode == 'ebook_only') else 'audiobook'
                active_clients = {
                    name: client for name, client in self.sync_clients.items()
                    if sync_type in client.get_supported_sync_types()
                }
                if sync_type == 'ebook':
                    logger.debug(f"[{abs_id}] [{title_snip}] Ebook-only mode - using clients: {list(active_clients.keys())}")

                # Build config using active_clients - parallel fetch
                config = self._fetch_states_parallel(book, prev_states_by_client, title_snip, bulk_states_per_client, active_clients)

                # Filtered config now only contains non-None states
                if not config:
                    continue  # No valid states to process

                # Check for ABS offline condition (only for audiobook mode)
                # Check for ABS offline condition (only for audiobook mode)
                if not (hasattr(book, 'sync_mode') and book.sync_mode == 'ebook_only'):
                    abs_state = config.get('ABS')
                    if abs_state is None:
                        # Fallback logic: If ABS is missing but we have ebook clients, try to sync them as ebook-only
                        ebook_clients_active = [k for k in config.keys() if k != 'ABS']
                        if ebook_clients_active:
                             logger.info(f"[{abs_id}] [{title_snip}] ABS audiobook not found/offline, falling back to ebook-only sync between {ebook_clients_active}")
                        else:
                             logger.debug(f"[{abs_id}] [{title_snip}] ABS audiobook offline and no other clients, skipping")
                             continue  # ABS offline and no fallback possible



                # Check for sync delta threshold between clients
                progress_values = [cfg.current.get('pct', 0) for cfg in config.values() if cfg.current.get('pct') is not None]
                significant_diff = False

                if len(progress_values) >= 2:
                    max_progress = max(progress_values)
                    min_progress = min(progress_values)
                    progress_diff = max_progress - min_progress

                    if progress_diff >= self.sync_delta_between_clients:
                        significant_diff = True
                        # If we have a significant diff, we verify it's not just noise
                        # by checking if we have at least one valid state
                        logger.debug(f"[{abs_id}] [{title_snip}] Detected discrepancies between clients ({progress_diff:.2%}), forcing sync check even if deltas are 0")
                        logger.debug(f"[{abs_id}] [{title_snip}] Client discrepancy detected: {min_progress:.1%} to {max_progress:.1%}")
                    else:
                        logger.debug(f"[{abs_id}] [{title_snip}] Progress difference {progress_diff:.2%} below threshold {self.sync_delta_between_clients:.2%} - skipping sync")
                        # Do not continue here, let the consolidated check handle it

                # Check for Character Delta Threshold (Fix 2B)
                # Loop through ebook clients (KoSync, Storyteller, BookLore, ABS_Ebook)
                # If state.delta > 0 and book has epub, get total chars via extract_text_and_map
                # Calculate char_delta = int(state.delta * total_chars)
                # If char_delta >= self.delta_chars_thresh, log it and set significant_diff = True
                char_delta_triggered = False  # Track if character delta triggered significance
                if not significant_diff and hasattr(book, 'ebook_filename') and book.ebook_filename:
                    for client_name_key, client_state in config.items():
                         if client_state.delta > 0:
                             try:
                                 # Ensure file is available locally (download if needed)
                                 epub_path = self._get_local_epub(book.ebook_filename)
                                 if not epub_path:
                                     logger.warning(f"Could not locate or download EPUB for {book.ebook_filename}")
                                     continue

                                 # Use existing ebook_parser which has caching
                                 full_text, _ = self.ebook_parser.extract_text_and_map(epub_path)
                                 if full_text:
                                     total_chars = len(full_text)
                                     char_delta = int(client_state.delta * total_chars)

                                     if char_delta >= self.delta_chars_thresh:
                                         logger.info(f"[{abs_id}] [{title_snip}] Significant character change detected for {client_name_key}: {char_delta} chars (Threshold: {self.delta_chars_thresh})")
                                         significant_diff = True
                                         char_delta_triggered = True  # Mark that this came from char delta
                                         break
                             except Exception as e:
                                 logger.warning(f"Failed to check char delta for {client_name_key}: {e}")

                # Check if all 'delta' fields in config are zero
                # We typically skip if nothing changed, BUT if there is a significant discrepancy
                # between clients (e.g. from a fresh push to DB), we must proceed to sync them.
                deltas_zero = all(round(cfg.delta, 4) == 0 for cfg in config.values())
                
                # Check if any client has a significant delta (using time-based threshold)
                any_significant_delta = any(
                    self._has_significant_delta(k, config, book) 
                    for k in config.keys()
                )

                # If nothing changed AND clients are effectively in sync, skip
                if deltas_zero and not significant_diff:
                    logger.debug(f"[{abs_id}] [{title_snip}] No changes and clients in sync, skipping")
                    continue
                
                # If there's a discrepancy but no client actually changed, skip
                # (discrepancy will resolve next time someone reads)
                # Exception: if character delta triggered, we have a real change
                if significant_diff and not any_significant_delta and not char_delta_triggered:
                    logger.debug(f"[{abs_id}] [{title_snip}] Discrepancy exists ({max_progress*100:.1f}% vs {min_progress*100:.1f}%) but no recent client activity detected. Waiting for a new read event to determine true leader.")
                    continue

                if significant_diff:
                    logger.debug(f"[{abs_id}] [{title_snip}] Proceeding due to client discrepancy")

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
                        small_changes.append(f"✋ [{abs_id}] [{title_snip}] {label} delta {delta_str} (Below threshold)")

                if small_changes and not any(cfg.delta >= cfg.threshold for cfg in config.values()):
                    # If we have significant discrepancies between clients, we MUST NOT skip,
                    # even if individual deltas are small (e.g. from DB pre-update).
                    if significant_diff:
                        logger.debug(f"[{abs_id}] [{title_snip}] Proceeding with sync despite small deltas due to client discrepancies.")
                    else:
                        for s in small_changes:
                            logger.info(s)
                        # No further action for only-small changes
                        continue

                # At this point we have a significant change to act on
                logger.info(f"🔄 [{abs_id}] [{title_snip}] Change detected")

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

                # Determine leader
                leader, leader_pct = self._determine_leader(config, book, abs_id, title_snip)
                if not leader:
                    continue

                leader_formatter = config[leader].value_formatter

                leader_client = self.sync_clients[leader]
                leader_state = config[leader]

                # Get canonical text from leader
                txt = leader_client.get_text_from_current_state(book, leader_state)
                if not txt:
                    logger.warning(f"⚠️ [{abs_id}] [{title_snip}] Could not get text from leader {leader}")
                    continue

                # Get locator (percentage, xpath, etc) from text
                epub = book.ebook_filename
                locator = leader_client.get_locator_from_text(txt, epub, leader_pct)
                if not locator:
                    # Try fallback if enabled (e.g. look at previous segment)
                    if getattr(self.ebook_parser, 'useXpathSegmentFallback', False):
                        fallback_txt = leader_client.get_fallback_text(book, leader_state)
                        if fallback_txt and fallback_txt != txt:
                            logger.info(f"🔄 [{abs_id}] [{title_snip}] Primary text match failed. Trying previous segment fallback...")
                            locator = leader_client.get_locator_from_text(fallback_txt, epub, leader_pct)
                            if locator:
                                logger.info(f"✅ [{abs_id}] [{title_snip}] Fallback successful!")

                if not locator:
                    logger.warning(f"⚠️ [{abs_id}] [{title_snip}] Could not resolve locator from text for leader {leader}, falling back to percentage of leader.")
                    locator = LocatorResult(percentage=leader_pct)

                # Update all other clients and store results
                results: dict[str, SyncResult] = {}
                for client_name, client in self.sync_clients.items():
                    if client_name == leader:
                        continue

                    # Skip ABS update if in ebook-only mode
                    if client_name == 'ABS' and hasattr(book, 'sync_mode') and book.sync_mode == 'ebook_only':
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
                        logger.info(f"[{abs_id}] [{title_snip}] Updated state data for {client_name}: " + str(state_data))
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

                logger.info(f"💾 [{abs_id}] [{title_snip}] States saved to database")

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

            # Acquire lock to prevent race conditions with active sync cycles
            with self._sync_lock:
                # Get the book first
                book = self.database_service.get_book(abs_id)
                if not book:
                    raise ValueError(f"Book not found: {abs_id}")

                # Clear all states for this book from database
                cleared_count = self.database_service.delete_states_for_book(abs_id)
                logger.info(f"💾 Cleared {cleared_count} state records from database")

                # Delete KOSync document record to bypass "furthest wins" protection
                # Without this, the integrated KOSync server will reject the 0% update
                # and the old progress will sync back on the next cycle
                if book.kosync_doc_id:
                    deleted = self.database_service.delete_kosync_document(book.kosync_doc_id)
                    if deleted:
                        logger.info(f"🗑️ Deleted KOSync document record: {book.kosync_doc_id[:8]}...")

                # Reset all sync clients to 0% progress
                reset_results = {}
                locator = LocatorResult(percentage=0.0)
                request = UpdateProgressRequest(locator_result=locator, txt="", previous_location=None)

                for client_name, client in self.sync_clients.items():
                    if client_name == 'ABS' and book.sync_mode == 'ebook_only':
                        logger.debug(f"[{book.abs_title}] Ebook-only mode - skipping ABS progress reset")
                        continue
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

                # Force the background job to re-evaluate this book (e.g. to generate missing alignment maps)
                book.status = 'pending'
                self.database_service.save_book(book)
                logger.info(f"   [JOB] Book marked as 'pending' to trigger alignment check.")

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

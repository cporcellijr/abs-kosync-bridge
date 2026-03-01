import logging
import threading
import shutil
import time
import os
import html
from pathlib import Path
from urllib.parse import urljoin
import requests

from src.services.alignment_service import ingest_storyteller_transcripts
from src.utils.storyteller_transcript import StorytellerTranscript

logger = logging.getLogger(__name__)

class ForgeService:
    def __init__(self, database_service, abs_client, booklore_client, storyteller_client, library_service, ebook_parser, transcriber, alignment_service):
        self.database_service = database_service
        self.abs_client = abs_client
        self.booklore_client = booklore_client
        self.storyteller_client = storyteller_client
        self.library_service = library_service
        self.ebook_parser = ebook_parser
        self.transcriber = transcriber
        self.alignment_service = alignment_service
        self.active_tasks = set()
        self.lock = threading.Lock()
        
        # Load environment variables
        self.ABS_API_TOKEN = os.environ.get("ABS_KEY")
        self.ABS_API_URL = os.environ.get("ABS_SERVER")
        self.ABS_AUDIO_ROOT = Path(os.environ.get("AUDIOBOOKS_DIR", "/audiobooks"))

    @staticmethod
    def safe_folder_name(name: str) -> str:
        invalid = '<>:"/\\|?*'
        name = html.escape(str(name).strip())[:150]
        for c in invalid:
            name = name.replace(c, '_')
        return name.strip() or "Unknown"

    def _copy_audio_files(self, abs_id: str, dest_folder: Path):
        """Copy audiobook files from ABS - Book Linker version"""
        headers = {"Authorization": f"Bearer {self.ABS_API_TOKEN}"}
        url = urljoin(self.ABS_API_URL, f"/api/items/{abs_id}")
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            item = r.json()
            audio_files = item.get("media", {}).get("audioFiles", [])
            if not audio_files:
                logger.warning(f"⚠️ No audio files found for ABS '{abs_id}'")
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
                if not src_path and full_path:
                    parts = Path(full_path).parts
                    for i in range(4, 0, -1):
                        if len(parts) < i: continue
                        suffix = Path(*parts[-i:])
                        candidate = self.ABS_AUDIO_ROOT / suffix
                        if candidate.exists():
                            src_path = candidate
                            break

                # 3. Filename fallback
                if not src_path and filename:
                    matches = list(self.ABS_AUDIO_ROOT.glob(f"**/{filename}"))
                    if matches:
                        src_path = matches[0]

                if src_path and src_path.exists():
                    shutil.copy2(str(src_path), dest_folder / src_path.name)
                    copied += 1
                else:
                    # 4. API Download Fallback
                    logger.info(f"⚡ Local file not found, downloading via API: '{filename}'")
                    stream_url = f"{self.ABS_API_URL.rstrip('/')}/api/items/{abs_id}/file/{f.get('ino')}?token={self.ABS_API_TOKEN}"
                    dest_path = dest_folder / filename
                    # Use the ABS Client
                    if self.abs_client.download_file(stream_url, dest_path):
                        copied += 1
                    else:
                        logger.error(f"❌ Could not find or download audio file: '{filename}'")
            
            if copied == len(audio_files):
                return True
            else:
                logger.error(f"❌ Forge Strict Check Failed: Expected {len(audio_files)} files, copied {copied} — Aborting")
                return False
        except Exception as e:
            logger.error(f"❌ Failed to copy ABS '{abs_id}': {e}", exc_info=True)
            return False

    def start_manual_forge(self, abs_id, text_item, title, author):
        """
        Start manual forge process in background thread.
        """
        thread = threading.Thread(
            target=self._forge_background_task,
            args=(abs_id, text_item, title, author),
            daemon=True
        )
        thread.start()

    def _forge_background_task(self, abs_id, text_item, title, author):
        """
        Background thread: copy files to Storyteller library, trigger processing, cleanup.
        """
        logger.info(f"🔨 Forge: Starting background task for '{title}'")
        
        with self.lock:
            self.active_tasks.add(title)

        try:
            safe_author = self.safe_folder_name(author) if author else "Unknown"
            safe_title = self.safe_folder_name(title) if title else "Unknown"
            
            st_lib_path = Path(os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))

            final_course_dir = st_lib_path / safe_title
            hidden_staging_dir = st_lib_path / f".staging_{safe_title}"

            if final_course_dir.exists():
                logger.warning(f"⚠️ Target directory '{final_course_dir}' already exists. Using it directly")
                course_dir = final_course_dir
            else:
                course_dir = hidden_staging_dir
                course_dir.mkdir(parents=True, exist_ok=True)

            audio_dest = course_dir
            
            logger.info(f"⚡ Forge: Staging files for '{title}' in '{course_dir}' (Atomic)")

            # Step 1: Copy audio files
            audio_ok = self._copy_audio_files(abs_id, audio_dest)
            if not audio_ok:
                logger.error(f"❌ Forge: Failed to copy audio files for '{abs_id}'")
                try:
                    if course_dir.exists() and course_dir != final_course_dir: 
                        shutil.rmtree(course_dir) 
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
                    logger.error(f"❌ Forge: Local file not found: '{src_path}'")

            elif source == 'Booklore':
                booklore_id = text_item.get('booklore_id')
                if booklore_id:
                    content = self.booklore_client.download_book(booklore_id)
                    if content:
                        epub_dest.write_bytes(content)
                        text_success = True
                        logger.info(f"⚡ Forge: Booklore epub downloaded")
                    else:
                        logger.error(f"❌ Forge: Booklore download failed for '{booklore_id}'")

            elif source == 'ABS':
                abs_item_id = text_item.get('abs_id')
                if abs_item_id:
                    ebook_files = self.abs_client.get_ebook_files(abs_item_id)
                    if ebook_files:
                        stream_url = ebook_files[0].get('stream_url', '')
                        if stream_url and self.abs_client.download_file(stream_url, epub_dest):
                            text_success = True
                            logger.info(f"⚡ Forge: ABS epub downloaded")
                        else:
                            logger.error(f"❌ Forge: ABS download failed for '{abs_item_id}'")
            
            elif source == 'CWA':
                download_url = text_item.get('download_url', '')
                cwa_id = text_item.get('cwa_id')
                cwa_client = self.library_service.cwa_client
                
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
                    logger.error(f"❌ Forge: CWA download failed")

            else:
                logger.error(f"❌ Forge: Unknown text source: '{source}'")

            if not text_success:
                logger.error(f"❌ Forge: Text acquisition failed — Aborting")
                try:
                    if course_dir.exists() and course_dir != final_course_dir:
                        shutil.rmtree(course_dir)
                except: pass
                return

            # Atomic reveal (staging already inside Storyteller volume).
            if course_dir != final_course_dir:
                try:
                    if final_course_dir.exists():
                        shutil.rmtree(final_course_dir)


                    logger.info(f"⚡ Forge: Atomically revealing folder to Storyteller scanner...")
                    hidden_staging_dir.rename(final_course_dir)
                    # Keep a placeholder at the old path so scanner callbacks
                    # that already captured '.staging_*' don't fail with ENOENT.
                    hidden_staging_dir.mkdir(parents=True, exist_ok=True)
                    course_dir = final_course_dir
                except Exception as e:
                    logger.error(f"❌ Forge: Atomic transfer failed: {e}")
                    try: shutil.rmtree(hidden_staging_dir)
                    except: pass
                    raise Exception(f"Atomic move failed: {e}")

            logger.info(f"⚡ Forge: Files staged. Waiting for Storyteller to detect '{title}'...")

            # Trigger Storyteller Processing via API
            st_client = self.storyteller_client
            found_uuid = None
            epub_filename = f"{safe_title}.epub"

            for _ in range(240):
                time.sleep(5)
                try:
                    # Primary: match by staged file path (deterministic)
                    found_uuid = st_client.find_book_by_staged_path(safe_title, epub_filename)

                    # Fallback: title search
                    if not found_uuid:
                        results = st_client.search_books(title)
                        for b in results:
                            if b.get('title') == title:
                                found_uuid = b.get('uuid') or b.get('id')
                                break

                    if found_uuid:
                        logger.info(f"⚡ Forge: Book detected ({found_uuid}). Waiting 60s for internal EPUB linking...")
                        time.sleep(60)
                        break
                except Exception as e:
                    logger.debug(f"Forge: Storyteller detection error (retrying): {e}")

            if found_uuid:
                logger.info(f"⚡ Forge: Book detected ({found_uuid}). Triggering processing...")
                try:
                    if hasattr(st_client, 'trigger_processing'):
                        st_client.trigger_processing(found_uuid)
                    else:
                        logger.warning("⚠️ Storyteller client missing trigger_processing method")
                except Exception as e:
                     logger.error(f"❌ Forge: Failed to trigger processing: {e}")
            else:
                logger.warning(f"⚠️ Forge: Storyteller scan timed out — Processing might happen automatically later")


            # Step 3: Cleanup Monitor
            AUDIO_EXTENSIONS = {'.mp3', '.m4b', '.m4a', '.flac', '.ogg', '.opus', '.wma', '.wav', '.aac'}
            MAX_WAIT = 3600  # 60 minutes
            POLL_INTERVAL = 30 # Check every 30s
            elapsed = 0

            logger.info(f"⚡ Forge: Starting cleanup monitor (polling every {POLL_INTERVAL}s, max {MAX_WAIT}s)")

            while elapsed < MAX_WAIT:
                time.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL

                try:
                    readaloud_files = list(course_dir.glob("*readaloud*.epub")) + list(course_dir.glob("*synced*/*.epub"))
                    
                    if readaloud_files:
                        logger.info(f"⚡ Forge: Readaloud detected: {readaloud_files[0].name}")

                        # [SAFETY CHECK]
                        if found_uuid:
                            try:
                                logger.info(f"⚡ Forge: Verifying processing status for {found_uuid}...")
                                for _ in range(12): 
                                    details = st_client.get_book_details(found_uuid)
                                    time.sleep(5)
                                
                                logger.info("⚡ Forge: Safety delay (60s) to allow Storyteller to release file locks...")
                                time.sleep(60) 
                            except Exception as e:
                                logger.warning(f"⚠️ Forge: Safety check failed: {e} — Proceeding with caution")
                                time.sleep(30)

                        # --- EXTRACT & ALIGN ---
                        completed_epub_path = readaloud_files[0]
                        try:
                            logger.info(f"⚡ Forge: Extracting SMIL transcript from {completed_epub_path.name}...")
                            item_details = self.abs_client.get_item_details(abs_id)
                            chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
                            book_text, _ = self.ebook_parser.extract_text_and_map(completed_epub_path)
                            raw_transcript = self.transcriber.transcribe_from_smil(
                                abs_id, completed_epub_path, chapters, full_book_text=book_text
                            )
                            if not raw_transcript:
                                logger.error(f"❌ Forge: SMIL extraction returned no transcript for '{abs_id}' — Alignment map not created")
                            else:
                                success = self.alignment_service.align_and_store(abs_id, raw_transcript, book_text, chapters)
                                if not success:
                                    logger.error(f"❌ Forge: align_and_store failed for '{abs_id}' — Alignment map not created")
                                else:
                                    logger.info(f"✅ Forge: Alignment map stored for '{abs_id}'")
                        except Exception as e:
                            logger.error(f"❌ Forge: Alignment extraction failed: {e}")

                        deleted = 0
                        for f in course_dir.iterdir():
                            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                                try:
                                    f.unlink()
                                    deleted += 1
                                except Exception: pass

                        if epub_dest.exists() and epub_dest not in readaloud_files:
                            try:
                                epub_dest.unlink()
                                deleted += 1
                            except Exception: pass

                        logger.info(f"⚡ Forge: Cleanup complete - deleted {deleted} source files.")
                        return

                except Exception as e:
                    logger.warning(f"⚠️ Forge: Cleanup monitor error: {e}")

            logger.warning(f"⚠️ Forge: Cleanup monitor timed out after {MAX_WAIT}s for '{title}' — Source files remain")

        except Exception as e:
            logger.error(f"❌ Forge: Background task failed for '{title}': {e}", exc_info=True)
        finally:
            with self.lock:
                self.active_tasks.discard(title)

    def start_auto_forge_match(self, abs_id, text_item, title, author, original_filename, original_hash):
        """
        Start Auto-Forge & Match pipeline in background thread.
        Links forged artifact to DB after completion.
        """
        thread = threading.Thread(
            target=self._auto_forge_background_task,
            args=(abs_id, text_item, title, author, original_filename, original_hash),
            daemon=True
        )
        thread.start()

    def _auto_forge_background_task(self, abs_id, text_item, title, author, original_filename, original_hash):
        """
        Background task for Auto-Forge & Match pipeline.
        Staging -> Trigger -> Wait -> Download -> Sanitize -> Recalc Hash -> Update DB -> Cleanup
        """
        logger.info(f"🔨 Auto-Forge: Starting pipeline for '{title}' (ABS {abs_id})")
        
        with self.lock:
            self.active_tasks.add(title)

        try:
            # --- STAGING & TRIGGER ---
            safe_author = self.safe_folder_name(author) if author else "Unknown"
            safe_title = self.safe_folder_name(title) if title else "Unknown"
            st_lib_path = Path(os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))

            final_course_dir = st_lib_path / safe_title
            hidden_staging_dir = st_lib_path / f".staging_{safe_title}"

            if final_course_dir.exists():
                logger.warning(f"⚠️ Target directory '{final_course_dir}' already exists. Using it directly")
                course_dir = final_course_dir
            else:
                course_dir = hidden_staging_dir
                course_dir.mkdir(parents=True, exist_ok=True)
            
            # Copy Audio
            if not self._copy_audio_files(abs_id, course_dir):
                raise Exception("Failed to copy audio files")
                
            # Copy Text
            epub_dest = course_dir / f"{safe_title}.epub"
            source = text_item.get('source')
            if source == 'Local File':
                shutil.copy2(text_item.get('path'), epub_dest)
            elif source == 'Booklore':
                content = self.booklore_client.download_book(text_item.get('booklore_id'))
                if content: epub_dest.write_bytes(content)
            elif source == 'ABS':
                 ebook_files = self.abs_client.get_ebook_files(text_item.get('abs_id'))
                 if ebook_files: self.abs_client.download_file(ebook_files[0]['stream_url'], epub_dest)
            elif source == 'CWA':
                 cwa_client = getattr(self.library_service, 'cwa_client', None)
                 download_url = text_item.get('download_url')
                 cwa_id = text_item.get('cwa_id')
                 text_downloaded = False
                 if download_url and cwa_client:
                     text_downloaded = bool(cwa_client.download_ebook(download_url, epub_dest))
                 elif cwa_id and cwa_client:
                     book_info = cwa_client.get_book_by_id(cwa_id)
                     if book_info and book_info.get('download_url'):
                         text_downloaded = bool(cwa_client.download_ebook(book_info['download_url'], epub_dest))
                 if not text_downloaded:
                     logger.error(f"❌ Auto-Forge: CWA download failed for '{cwa_id or download_url or 'unknown'}'")
            else:
                 raise Exception(f"Unknown or missing text source type: '{source}'")
            
            if not epub_dest.exists():
                raise Exception("Failed to acquire text source")

            # Atomic reveal (staging already inside Storyteller volume).
            if course_dir != final_course_dir:
                try:
                    if final_course_dir.exists():
                        shutil.rmtree(final_course_dir)


                    logger.info(f"⚡ Forge: Atomically revealing folder to Storyteller scanner...")
                    hidden_staging_dir.rename(final_course_dir)
                    # Keep a placeholder at the old path so scanner callbacks
                    # that already captured '.staging_*' don't fail with ENOENT.
                    hidden_staging_dir.mkdir(parents=True, exist_ok=True)
                    course_dir = final_course_dir
                except Exception as e:
                    logger.error(f"❌ Forge: Atomic transfer failed: {e}")
                    try: shutil.rmtree(hidden_staging_dir)
                    except: pass
                    raise Exception(f"Atomic move failed: {e}")

            logger.info("⚡ Auto-Forge: Files staged. Waiting for Storyteller detection...")

            # Trigger Storyteller
            st_client = self.storyteller_client
            found_uuid = None
            epub_filename = f"{safe_title}.epub"

            for _ in range(240): # Wait up to 20 mins for initial detection
                time.sleep(5)
                try:
                    # Primary: match by staged file path (deterministic)
                    found_uuid = st_client.find_book_by_staged_path(safe_title, epub_filename)

                    # Fallback: title search
                    if not found_uuid:
                        results = st_client.search_books(title)
                        for b in results:
                            if b.get('title') == title:
                                found_uuid = b.get('uuid') or b.get('id')
                                break

                    if found_uuid:
                        logger.info(f"⚡ Forge: Book detected ({found_uuid}). Waiting 60s for internal EPUB linking...")
                        time.sleep(60)
                        break
                except Exception as e:
                    logger.debug(f"Forge: Storyteller detection error (retrying): {e}")

            if found_uuid:
                logger.info(f"⚡ Auto-Forge: Triggering processing for {found_uuid}")
                st_client.trigger_processing(found_uuid)
            else:
                logger.warning("⚠️ Auto-Forge: Storyteller scan timed out — Proceeding anyway in hopes it picks up")

            # --- WAIT FOR COMPLETION ---
            MAX_WAIT = 3600
            elapsed = 0
            readaloud_found = False
            
            while elapsed < MAX_WAIT:
                time.sleep(30)
                elapsed += 30
                
                # Check for readaloud
                readaloud_files = list(course_dir.glob("*readaloud*.epub")) + list(course_dir.glob("*synced*/*.epub"))
                if readaloud_files:
                    readaloud_found = True
                    break
            
            if not readaloud_found:
                 raise Exception("Timeout waiting for Storyteller processing")

            readaloud_path = readaloud_files[0] if readaloud_files else None

            # Safety Wait
            time.sleep(60)

            # --- DOWNLOAD ---
            logger.info("⚡ Auto-Forge: Processing complete. Downloading artifact...")
            epub_cache = self.ebook_parser.epub_cache_dir
            if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)

            target_filename = f"storyteller_{found_uuid}.epub"
            target_path = epub_cache / target_filename

            try:
                if not st_client.download_book(found_uuid, target_path):
                    raise Exception("API download returned False")
            except Exception as api_err:
                if readaloud_path and readaloud_path.exists():
                    logger.warning(f"⚠️ Auto-Forge: API download failed ({api_err}). Using local file: '{readaloud_path}'")
                    shutil.copy2(readaloud_path, target_path)
                else:
                    raise Exception(f"Failed to download Storyteller artifact and no local fallback available: {api_err}")


            # --- RECALCULATE HASH ---
            # [FIX] Prioritize original_hash if valid (Tri-Link Principle)
            if original_hash:
                 logger.info(f"⚡ Auto-Forge: Preserving Original Hash: {original_hash}")
                 new_hash = original_hash
            else:
                 new_hash = self.ebook_parser.get_kosync_id(target_path)
                 logger.info(f"⚡ Auto-Forge: Generated New Hash (Artifact): {new_hash}")

            # --- EXTRACT TEXT ---
            item_details = self.abs_client.get_item_details(abs_id)
            chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
            book_text, _ = self.ebook_parser.extract_text_and_map(target_path)

            # --- INGEST STORYTELLER TRANSCRIPT (PRIMARY) ---
            storyteller_manifest = ingest_storyteller_transcripts(abs_id, title, chapters)
            storyteller_alignment_ok = False
            if storyteller_manifest:
                logger.info(f"Auto-Forge: Storyteller transcript ingested for '{abs_id}'")
                try:
                    st_transcript = StorytellerTranscript(storyteller_manifest)
                    if self.alignment_service.align_storyteller_and_store(abs_id, st_transcript, ebook_text=book_text):
                        storyteller_alignment_ok = True
                        logger.info(f"Auto-Forge: Storyteller-anchored alignment map stored for '{abs_id}'")
                    else:
                        logger.warning(f"Auto-Forge: Storyteller alignment failed, falling back to SMIL for '{abs_id}'")
                except Exception as st_err:
                    logger.warning(f"Auto-Forge: Storyteller alignment error ({st_err}), falling back to SMIL for '{abs_id}'")
            else:
                logger.info(f"Auto-Forge: No Storyteller transcript files found for '{abs_id}'")

            # --- SMIL FALLBACK (LAST RESORT) ---
            if not storyteller_alignment_ok:
                logger.info("Auto-Forge: Falling back to SMIL transcript extraction...")
                raw_transcript = self.transcriber.transcribe_from_smil(
                    abs_id, target_path, chapters, full_book_text=book_text
                )
                if not raw_transcript:
                    raise Exception('Auto-Forge: SMIL extraction returned no transcript. Cannot build alignment map.')
                success = self.alignment_service.align_and_store(abs_id, raw_transcript, book_text, chapters)
                if not success:
                    raise Exception('Auto-Forge: align_and_store failed to generate a valid alignment map.')
                logger.info(f"Auto-Forge: SMIL alignment map stored for '{abs_id}'")

            # --- UPDATE DATABASE ---
            # NOTE: DB service calls need connection. Assuming database_service handles its own session.
            book = self.database_service.get_book(abs_id)
            if book:
                book.ebook_filename = target_filename
                book.storyteller_uuid = found_uuid
                book.kosync_doc_id = new_hash
                book.status = 'active'
                if storyteller_manifest:
                    book.transcript_file = storyteller_manifest
                    book.transcript_source = 'storyteller'
                self.database_service.save_book(book)
                logger.info(f"✅ Auto-Forge: Book {abs_id} updated successfully!")
            else:
                logger.error(f"❌ Auto-Forge: Book {abs_id} not found in DB to update!")

            # --- ADD TO COLLECTIONS/SHELVES ---
            try:
                abs_collection_name = os.environ.get("ABS_COLLECTION_NAME", "Synced with KOReader")
                self.abs_client.add_to_collection(abs_id, abs_collection_name)

                if self.booklore_client:
                    shelf_filename = original_filename if original_filename else target_filename
                    booklore_shelf_name = os.environ.get("BOOKLORE_SHELF_NAME", "Kobo")
                    self.booklore_client.add_to_shelf(shelf_filename, booklore_shelf_name)

                if self.storyteller_client:
                    if found_uuid and hasattr(self.storyteller_client, 'add_to_collection_by_uuid'):
                        self.storyteller_client.add_to_collection_by_uuid(found_uuid)
                    else:
                        self.storyteller_client.add_to_collection(target_filename)
                    
            except Exception as e:
                logger.warning(f"⚠️ Auto-Forge: Failed to add to collections/shelves: {e}")

            # --- CLEANUP ---
            AUDIO_EXTENSIONS = {'.mp3', '.m4b', '.m4a', '.flac', '.ogg', '.opus', '.wma', '.wav', '.aac'}
            for f in course_dir.iterdir():
                if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                    try: f.unlink()
                    except: pass
            if epub_dest.exists(): 
                try: epub_dest.unlink()
                except: pass

        except Exception as e:
            logger.error(f"❌ Auto-Forge: Pipeline failed: {e}", exc_info=True)
            try:
                book = self.database_service.get_book(abs_id)
                if book:
                    book.status = 'error'
                    self.database_service.save_book(book)
            except: pass
            
        finally:
             with self.lock:
                self.active_tasks.discard(title)

import os
import time
import json
import schedule
import logging
import sys
from pathlib import Path
from rapidfuzz import process, fuzz
from zipfile import ZipFile
import lxml.etree as ET

# Import local modules
from api_clients import ABSClient, KoSyncClient
from transcriber import AudioTranscriber
from ebook_utils import EbookParser
from storyteller_db import StorytellerDB  # NEW: Import Storyteller DB client

import logging
import os

# Add trace level logging
TRACE_LEVEL_NUM = 5
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")
logging.TRACE = TRACE_LEVEL_NUM
def trace(self, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)

logging.Logger.trace = trace

# Read user defined debug lecel, default to INFO. Check its an acual level other wise default INFO
env_log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
try:
    log_level = getattr(logging, env_log_level)
except AttributeError:
    log_level = logging.INFO 

logging.basicConfig(
    level=log_level, 
    format='%(asctime)s %(levelname)s: %(message)s', 
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

DATA_DIR = Path("/data")
BOOKS_DIR = Path("/books")
DB_FILE = DATA_DIR / "mapping_db.json"
STATE_FILE = DATA_DIR / "last_state.json"

class SyncManager:
    def __init__(self):
        logger.info("Initializing Sync Manager...")
        self.abs_client = ABSClient()
        self.kosync_client = KoSyncClient()
        self.storyteller_db = StorytellerDB()  # NEW: Initialize Storyteller DB
        self.transcriber = AudioTranscriber(DATA_DIR)
        self.ebook_parser = EbookParser(BOOKS_DIR)
        self.db = self._load_db()
        self.state = self._load_state()
        
        # Load Sync Thresholds
        # ABS: Seconds (Default 60s)
        self.delta_abs_thresh = float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60))
        # KoSync: Percentage (Default 1%) -> Converted to 0.01
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0
        # Kosync: Character (Default 400 Words) -> Converted to characters by multiplying by 5
        self.delta_kosync_char_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_WORDS", 400)) * 5
        
        logger.info(f"[CONFIG] Sync Thresholds: ABS={self.delta_abs_thresh}s, KoSync={self.delta_kosync_thresh:.2%} ({self.delta_kosync_char_thresh} chars)")
        
        self.startup_checks()
        self.cleanup_stale_jobs()

    def startup_checks(self):
        logger.info("--- Performing Connectivity Checks ---")
        abs_ok = self.abs_client.check_connection()
        kosync_ok = self.kosync_client.check_connection()
        storyteller_ok = self.storyteller_db.check_connection()  # NEW: Check Storyteller DB
        
        if not abs_ok: logger.warning("[WARN] Audiobookshelf connection FAILED.")
        if not kosync_ok: logger.warning("[WARN] KoSync connection FAILED.")
        if not storyteller_ok: logger.warning("[WARN] Storyteller DB connection FAILED.")

    def _load_db(self):
        if DB_FILE.exists():
            with open(DB_FILE, 'r') as f:
                return json.load(f)
        return {"mappings": []}

    def _save_db(self):
        with open(DB_FILE, 'w') as f:
            json.dump(self.db, f, indent=2)

    def _load_state(self):
        if STATE_FILE.exists():
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        return {}

    def _save_state(self):
        with open(STATE_FILE, 'w') as f:
            json.dump(self.state, f, indent=2)

    def _get_abs_title(self, item):
        """Helper method to extract title from ABS audiobook item"""
        title = item.get('media', {}).get('metadata', {}).get('title')
        if not title: 
            title = item.get('name')
        if not title: 
            title = item.get('title')
        return title or "Unknown Title"

    def cleanup_stale_jobs(self):
        """Reset crashed jobs to active on startup"""
        changed = False
        for mapping in self.db['mappings']:
            if mapping.get('status') == 'crashed':
                mapping['status'] = 'active'
                changed = True
                logger.info(f"Reset crashed job: {mapping.get('abs_title', 'Unknown')}")
        if changed:
            self._save_db()

    def get_text_from_storyteller_fragment(self, ebook_filename, href, fragment_id):
        """
        Extracts exact text from EPUB using Storyteller's fragment ID.
        
        Args:
            ebook_filename: Name of the ebook file
            href: Internal EPUB file path (e.g., "OPS/s065-Chapter-048.xhtml")
            fragment_id: Sentence ID (e.g., "s065-sentence186")
        
        Returns:
            str: The exact text from that fragment, or None if not found
        """
        if not href or not fragment_id:
            return None
        
        try:
            # Find the epub file path
            epub_path = None
            for f in BOOKS_DIR.rglob(ebook_filename):
                epub_path = f
                break
            
            if not epub_path:
                logger.error(f"EPUB not found: {ebook_filename}")
                return None
            
            with ZipFile(epub_path, 'r') as zip_ref:
                # Storyteller hrefs might need path adjustment
                if href not in zip_ref.namelist():
                    # Try to find it with partial match
                    matching_files = [f for f in zip_ref.namelist() if href in f]
                    if matching_files:
                        href = matching_files[0]
                    else:
                        logger.error(f"File not found in EPUB: {href}")
                        return None
                
                with zip_ref.open(href) as f:
                    content = f.read()
                    parser = ET.HTMLParser(encoding='utf-8')
                    tree = ET.fromstring(content, parser)
                    
                    # Find element with the fragment ID
                    # XPath: //*[@id='s065-sentence186']
                    elements = tree.xpath(f"//*[@id='{fragment_id}']")
                    
                    if elements:
                        # Extract all text from this element and its children
                        text = "".join(elements[0].itertext()).strip()
                        logger.info(f"[XPATH] Exact text via fragment '{fragment_id}': '{text[:80]}...'")
                        return text
                    else:
                        logger.warning(f"Fragment ID not found in EPUB: {fragment_id}")
                        return None
                        
        except Exception as e:
            logger.error(f"Error extracting text from fragment: {e}")
            return None


    def add_mapping(self, abs_id, kosync_doc_id, transcript_file, ebook_filename, abs_title="Unknown"):
        mapping = {
            "abs_id": abs_id,
            "kosync_doc_id": kosync_doc_id,
            "transcript_file": str(transcript_file),
            "ebook_filename": ebook_filename,
            "abs_title": abs_title,
            "status": "pending_transcript"
        }
        self.db['mappings'].append(mapping)
        self._save_db()
        logger.info(f"Added mapping for '{abs_title}' (Status: pending_transcript)")

    def remove_mapping(self, abs_id):
        original_len = len(self.db['mappings'])
        self.db['mappings'] = [m for m in self.db['mappings'] if m['abs_id'] != abs_id]
        if len(self.db['mappings']) < original_len:
            self._save_db()
            if abs_id in self.state:
                del self.state[abs_id]
                self._save_state()
            logger.info(f"Removed mapping for ABS ID: {abs_id}")
            return True
        return False

    def check_pending_jobs(self):
        """Process pending jobs: download audio, transcribe, and activate sync"""
        self.db = self._load_db()
        for mapping in self.db['mappings']:
            status = mapping.get('status')
            # Check for both 'pending' (needs transcription) and 'pending_transcript' (waiting for existing transcript)
            if status == 'pending':
                # OLD WORKFLOW: Download and transcribe
                abs_title = mapping.get('abs_title', 'Unknown')
                logger.info(f"[START] Found pending job for: {abs_title}")
                
                mapping['status'] = 'processing'
                self._save_db()
                
                try:
                    audio_files = self.abs_client.get_audio_files(mapping['abs_id'])
                    if not audio_files:
                        logger.error(f"[FAIL] No audio files found for {abs_title}.")
                        mapping['status'] = 'failed'
                        self._save_db()
                        continue

                    logger.info("   Starting transcription...")
                    transcript_path = self.transcriber.process_audio(mapping['abs_id'], audio_files)
                    
                    logger.info("   Priming ebook cache...")
                    self.ebook_parser.extract_text_and_map(mapping['ebook_filename'])

                    mapping['transcript_file'] = str(transcript_path)
                    mapping['status'] = 'active'
                    self._save_db()
                    logger.info(f"[OK] Job complete! {abs_title} is now active and syncing.")

                except Exception as e:
                    logger.error(f"[FAIL] Job failed for {abs_title}: {e}")
                    mapping['status'] = 'failed_retry_later' 
                    self._save_db()
                    
            elif status == 'pending_transcript':
                # NEW WORKFLOW: Just wait for transcript to exist
                transcript_path = Path(mapping.get('transcript_file')) if mapping.get('transcript_file') else None
                abs_title = mapping.get('abs_title', 'Unknown')

                if not transcript_path:
                    continue

                if transcript_path.exists():
                    try:
                        logger.info(f"[SYNC] Transcript ready for '{abs_title}'. Activating sync...")
                        mapping['status'] = 'active'
                        self._save_db()
                        logger.info(f"[OK] {abs_title} is now active and syncing.")

                    except Exception as e:
                        logger.error(f"[FAIL] Job failed for {abs_title}: {e}")
                        mapping['status'] = 'failed_retry_later' 
                        self._save_db()

    def sync_cycle(self):
        """
        THREE-WAY SYNC LOGIC
        Syncs progress between ABS (audiobook), KoSync (ebook), and Storyteller DB
        """
        logger.debug("Starting Sync Cycle...")
        self.db = self._load_db() 
        
        if not self.db['mappings']: 
            return

        for mapping in self.db['mappings']:
            if mapping.get('status', 'active') != 'active': 
                continue
                
            abs_id = mapping['abs_id']
            kosync_id = mapping['kosync_doc_id']
            transcript_path = mapping['transcript_file']
            ebook_filename = mapping['ebook_filename']
            abs_title = mapping.get('abs_title', 'Unknown')

            # FETCH PROGRESS FROM ALL THREE SOURCES
            try:
                abs_progress = self.abs_client.get_progress(abs_id)  # Returns seconds
                kosync_progress = self.kosync_client.get_progress(kosync_id)  # Returns 0.0-1.0
                storyteller_progress, storyteller_ts = self.storyteller_db.get_progress(ebook_filename)  # Returns (percentage, timestamp) tuple
                
                # Handle None from Storyteller (no progress found)
                if storyteller_progress is None:
                    storyteller_progress = 0.0
                    storyteller_ts = 0
                    logger.debug(f"  [BOOK] No Storyteller progress found for '{ebook_filename}'")
                else:
                    logger.debug(f"[OK] Storyteller read: {abs_title} at {storyteller_progress*100:.2f}%")
                    
            except Exception as e:
                logger.error(f"Fetch failed for {abs_title}: {e}")
                continue

            # GET PREVIOUS STATE (with defaults for storyteller)
            defaults = {
                "abs_ts": 0, 
                "kosync_pct": 0, 
                "storyteller_pct": 0,  # NEW: Track Storyteller progress
                "last_updated": 0, 
                "kosync_index": 0
            }
            existing_data = self.state.get(abs_id, {})
            prev_state = defaults | existing_data
                
            # CALCULATE DELTAS
            abs_delta = abs(abs_progress - prev_state['abs_ts'])
            kosync_delta = abs(kosync_progress - prev_state['kosync_pct'])
            storyteller_delta = abs(storyteller_progress - prev_state['storyteller_pct'])  # NEW
            
            # DETERMINE WHAT CHANGED (based on thresholds)
            abs_changed = abs_delta > self.delta_abs_thresh
            kosync_changed = kosync_delta > self.delta_kosync_thresh
            storyteller_changed = storyteller_delta > self.delta_kosync_thresh  # NEW

            # Handle small ABS changes (below threshold)
            if abs_delta > 0 and not abs_changed:
                logger.info(f"  [BELOW_THRESH] ABS delta {abs_delta:.2f}s (Below threshold {self.delta_abs_thresh}s): {abs_title}")
                prev_state['abs_ts'] = abs_progress   
                prev_state['last_updated'] = time.time()
                prev_state['kosync_index'] = 0
                self.state[abs_id] = prev_state
                self._save_state()
                logger.info("  [SKIP] State matched to avoid loop.")
            
            # Handle small KoSync changes (check character delta too)
            if kosync_delta > 0 and not kosync_changed:
                logger.info(f"  [BELOW_THRESH] KoSync delta {kosync_delta:.4%} (Below threshold {self.delta_kosync_thresh:.2%}): {ebook_filename}")
                
                index_delta = self.ebook_parser.get_character_delta(ebook_filename, prev_state['kosync_pct'], kosync_progress)

                if index_delta > self.delta_kosync_char_thresh:
                    kosync_changed = True
                    logger.info(f"  [DELTA] KoSync character delta more than threshold {index_delta}/{self.delta_kosync_char_thresh}")
                else:  
                    logger.info(f"  [DELTA] KoSync character delta less than threshold {index_delta}/{self.delta_kosync_char_thresh}")
                    prev_state['kosync_pct'] = kosync_progress
                    prev_state['last_updated'] = time.time()
                    prev_state['kosync_index'] = 0
                    self.state[abs_id] = prev_state
                    self._save_state()
                    logger.info("  [SKIP] State matched to avoid loop.")

            # Handle small Storyteller changes (below threshold)
            if storyteller_delta > 0 and not storyteller_changed:
                logger.info(f"  [BELOW_THRESH] Storyteller delta {storyteller_delta:.4%} (Below threshold {self.delta_kosync_thresh:.2%}): {ebook_filename}")
                prev_state['storyteller_pct'] = storyteller_progress
                prev_state['last_updated'] = time.time()
                self.state[abs_id] = prev_state
                self._save_state()
                logger.info("  [SKIP] State matched to avoid loop.")

            # If nothing changed significantly, skip
            if not abs_changed and not kosync_changed and not storyteller_changed: 
                continue

            # DETERMINE SOURCE OF CHANGE
            logger.info(f"Change detected for '{abs_title}'")
            logger.info(f"  [STATS] ABS: {prev_state['abs_ts']:.2f}s -> {abs_progress:.2f}s (Delta=={abs_delta:.2f}s)")
            logger.info(f"  [STATS] KoSync: {prev_state['kosync_pct']:.4f}% -> {kosync_progress:.4f}% (Delta=={kosync_delta:.4f}%)")
            logger.info(f"  [STATS] Storyteller: {prev_state['storyteller_pct']:.4f}% -> {storyteller_progress:.4f}% (Delta=={storyteller_delta:.4f}%)")
            
            # ANTI-REGRESSION FAILSAFE
            # Prevent syncing backwards unless it's a small regression (chapter skip)
            REGRESSION_THRESHOLD = 0.05  # Allow 5% regression (might be legitimate)
            
            regression_detected = False
            regression_source = None
            
            # Check ABS regression
            if abs_changed and abs_progress < prev_state['abs_ts']:
                regression_amount_seconds = prev_state['abs_ts'] - abs_progress
                # Convert to percentage for comparison (estimate book length from current position)
                if prev_state['abs_ts'] > 0:
                    regression_pct = regression_amount_seconds / prev_state['abs_ts']
                    if regression_pct > REGRESSION_THRESHOLD:
                        logger.warning(f"  [WARN] ABS REGRESSION DETECTED: {prev_state['abs_ts']:.2f}s -> {abs_progress:.2f}s (-{regression_amount_seconds:.2f}s, -{regression_pct:.1%})")
                        regression_detected = True
                        regression_source = "ABS"
            
            # Check KoSync regression
            if kosync_changed and kosync_progress < prev_state['kosync_pct']:
                regression_amount = prev_state['kosync_pct'] - kosync_progress
                if regression_amount > REGRESSION_THRESHOLD:
                    logger.warning(f"  [WARN] KOSYNC REGRESSION DETECTED: {prev_state['kosync_pct']:.2%} -> {kosync_progress:.2%} (-{regression_amount:.2%})")
                    regression_detected = True
                    regression_source = "KOSYNC"
            
            # Check Storyteller regression
            if storyteller_changed and storyteller_progress < prev_state['storyteller_pct']:
                regression_amount = prev_state['storyteller_pct'] - storyteller_progress
                if regression_amount > REGRESSION_THRESHOLD:
                    logger.warning(f"  [WARN] STORYTELLER REGRESSION DETECTED: {prev_state['storyteller_pct']:.2%} -> {storyteller_progress:.2%} (-{regression_amount:.2%})")
                    regression_detected = True
                    regression_source = "STORYTELLER"
            
            # If regression detected, block the sync
            if regression_detected:
                logger.warning(f"  [BLOCKED] ANTI-REGRESSION: Blocking sync from {regression_source}")
                logger.warning(f"  [TIP] If restarting book, manually reset progress in all systems")
                # Update state to current values to prevent repeated warnings
                prev_state['abs_ts'] = abs_progress
                prev_state['kosync_pct'] = kosync_progress
                prev_state['storyteller_pct'] = storyteller_progress
                prev_state['last_updated'] = time.time()
                self.state[abs_id] = prev_state
                self._save_state()
                continue  # Skip this sync
            
            # Determine which source changed (priority: ABS > KoSync > Storyteller in case of conflict)
            if abs_changed:
                source = "ABS"
            elif kosync_changed:
                source = "KOSYNC"
            elif storyteller_changed:
                source = "STORYTELLER"
            else:
                source = "ABS"  # Fallback
            
            # Handle conflicts (when multiple sources changed)
            num_changed = sum([abs_changed, kosync_changed, storyteller_changed])
            if num_changed > 1:
                logger.warning(f"  [WARN] Conflict! {num_changed} sources changed. Defaulting to {source}.")

            # SYNC BASED ON SOURCE
            updated_ok = False
            try:
                # --- ABS CHANGED: Update KoSync and Storyteller ---
                if source == "ABS":
                    target_text = self.transcriber.get_text_at_time(transcript_path, abs_progress)
                    if target_text:
                        logger.info(f"  [SEARCH] Searching Ebook for text: '{target_text[:60]}...'")
                        logger.debug(f"  [SEARCH] Searching Ebook for text: '{target_text}'")
                        matched_pct, xpath, matched_index = self.ebook_parser.find_text_location(ebook_filename, target_text)
                        
                        if matched_pct is not None:
                            logger.info(f"  [OK] Match at {matched_pct:.2%}. Sending Updates...")

                            index_delta = abs(matched_index - prev_state['kosync_index'])
                            logger.info(f"  [DELTA] Index delta of {index_delta}.")
                            
                            # Update both KoSync and Storyteller
                            self.kosync_client.update_progress(kosync_id, matched_pct, xpath)
                            # Pass current time as timestamp for Storyteller conflict resolution
                            storyteller_updated = self.storyteller_db.update_progress(
                                ebook_filename, 
                                matched_pct,
                                source_timestamp=time.time()
                            )
                            
                            prev_state['abs_ts'] = abs_progress
                            prev_state['kosync_pct'] = matched_pct
                            if storyteller_updated:
                                prev_state['storyteller_pct'] = matched_pct  # Only update if successful
                            prev_state['kosync_index'] = matched_index
                            updated_ok = True
                        else:
                            logger.error("  [FAIL] Ebook text match FAILED.")
                
                # --- KOSYNC CHANGED: Update ABS and Storyteller ---
                elif source == "KOSYNC":
                    target_text = self.ebook_parser.get_text_at_percentage(ebook_filename, kosync_progress)
                    if target_text:
                        logger.info(f"  [SEARCH] Searching Transcript for text: '{target_text[:60]}...'")
                        matched_time = self.transcriber.find_time_for_text(transcript_path, target_text)
                        
                        if matched_time is not None:
                            logger.info(f"  [OK] Match at {matched_time:.2f}s. Sending Updates...")
                            
                            # Update both ABS and Storyteller
                            self.abs_client.update_progress(abs_id, matched_time)
                            # Pass current time as timestamp for Storyteller conflict resolution
                            storyteller_updated = self.storyteller_db.update_progress(
                                ebook_filename,
                                kosync_progress,
                                source_timestamp=time.time()
                            )
                            
                            prev_state['abs_ts'] = matched_time
                            prev_state['kosync_pct'] = kosync_progress
                            if storyteller_updated:
                                prev_state['storyteller_pct'] = kosync_progress  # Only update if successful
                            updated_ok = True
                        else:
                             logger.error("  [FAIL] Transcript text match FAILED.")
                
                # --- STORYTELLER CHANGED: Update ABS and KoSync ---
                elif source == "STORYTELLER":
                    # Check if Storyteller is behind the other systems
                    # If so, pull Storyteller forward instead of pushing others back
                    # This handles the case where you open a book in Storyteller for the first time
                    # but have already made progress in ABS/KoSync
                    
                    CATCHUP_THRESHOLD = 0.05  # 5% - if Storyteller is more than 5% behind, pull it forward
                    
                    furthest_pct = max(kosync_progress, prev_state.get('kosync_pct', 0))
                    storyteller_behind = (furthest_pct - storyteller_progress) > CATCHUP_THRESHOLD
                    
                    if storyteller_behind:
                        # Storyteller is behind - pull it forward to match the furthest system
                        logger.info(f"  [INFO] Storyteller ({storyteller_progress:.2%}) is behind KoSync ({furthest_pct:.2%})")
                        logger.info(f"  [SYNC] Pulling Storyteller forward instead of regressing others...")
                        
                        # Update Storyteller to match the furthest progress
                        storyteller_updated = self.storyteller_db.update_progress(
                            ebook_filename,
                            furthest_pct,
                            source_timestamp=time.time()
                        )
                        
                        if storyteller_updated:
                            prev_state['storyteller_pct'] = furthest_pct
                            updated_ok = True
                            logger.info(f"  [OK] Storyteller updated to {furthest_pct:.2%}")
                        else:
                            logger.error("  [FAIL] Failed to update Storyteller progress.")
                    else:
                        # Storyteller is ahead or roughly equal - normal sync flow
                        # Try to get precise text using fragment ID first
                        st_pct, st_ts, href, fragment_id = self.storyteller_db.get_progress_with_fragment(ebook_filename)
                        
                        MIN_TEXT_LENGTH = 100  # Minimum chars needed for reliable transcript matching
                        
                        target_text = None
                        if fragment_id:
                            # Try precise extraction using fragment
                            target_text = self.get_text_from_storyteller_fragment(ebook_filename, href, fragment_id)
                            if target_text and len(target_text) >= MIN_TEXT_LENGTH:
                                logger.info(f"  [OK] Using precise fragment-based text extraction ({len(target_text)} chars)")
                            else:
                                if target_text:
                                    logger.info(f"  [WARN] Fragment text too short ({len(target_text)} chars), need {MIN_TEXT_LENGTH}+")
                                target_text = None  # Force fallback
                        
                        # Fallback to percentage-based extraction if fragment method failed or text too short
                        if not target_text:
                            logger.info(f"  [INFO] Using percentage-based text extraction")
                            target_text = self.ebook_parser.get_text_at_percentage(ebook_filename, storyteller_progress)
                        
                        if target_text:
                            logger.info(f"  [SEARCH] Searching Transcript for text: '{target_text[:60]}...'")
                            matched_time = self.transcriber.find_time_for_text(transcript_path, target_text)
                            
                            if matched_time is not None:
                                logger.info(f"  [OK] Match at {matched_time:.2f}s. Sending Updates...")
                                
                                # Update both ABS and KoSync
                                self.abs_client.update_progress(abs_id, matched_time)
                                
                                # For KoSync, we need to generate XPath
                                _, xpath, matched_index = self.ebook_parser.find_text_location(ebook_filename, target_text)
                                self.kosync_client.update_progress(kosync_id, storyteller_progress, xpath)
                                
                                prev_state['abs_ts'] = matched_time
                                prev_state['kosync_pct'] = storyteller_progress
                                prev_state['storyteller_pct'] = storyteller_progress
                                prev_state['kosync_index'] = matched_index if matched_index else 0
                                updated_ok = True
                            else:
                                 logger.error("  [FAIL] Transcript text match FAILED.")

                # Save state if update was successful
                if updated_ok:
                    prev_state['last_updated'] = time.time()
                    self.state[abs_id] = prev_state
                    self._save_state()
                    logger.info("  [SAVED] State saved.")
                else:
                    # Update state to prevent loops even if sync failed
                    prev_state['abs_ts'] = abs_progress
                    prev_state['kosync_pct'] = kosync_progress
                    prev_state['storyteller_pct'] = storyteller_progress
                    prev_state['last_updated'] = time.time()
                    self.state[abs_id] = prev_state
                    self._save_state()
                    logger.info("  [SKIP] State matched to avoid loop.")
                    
            except Exception as e:
                logger.error(f"  Error syncing {abs_title}: {e}")

    def run_daemon(self):
        period = int(os.getenv("SYNC_PERIOD_MINS", 5))
        schedule.every(period).minutes.do(self.sync_cycle)
        schedule.every(1).minutes.do(self.check_pending_jobs)
        
        logger.info(f"Daemon running. Sync every {period} mins. Checking queue every 1 min.")
        self.sync_cycle()

        while True:
            schedule.run_pending()
            time.sleep(30)

if __name__ == "__main__":
    manager = SyncManager()
    manager.run_daemon()
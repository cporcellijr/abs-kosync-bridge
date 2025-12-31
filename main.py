"""
ABS-KoSync Bridge - Sync Daemon

HARDENED VERSION with:
- JsonDB for process-safe file locking
- Furthest-wins leader election
- Anti-regression protection
- Proper state management
"""

import os
import time
import json
import schedule
import logging
import sys
from pathlib import Path
from zipfile import ZipFile
import lxml.etree as ET

# Local modules
from json_db import JsonDB
from api_clients import ABSClient, KoSyncClient
from transcriber import AudioTranscriber
from ebook_utils import EbookParser
from storyteller_db import StorytellerDB

# --- Logging Configuration ---
TRACE_LEVEL_NUM = 5
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")
logging.TRACE = TRACE_LEVEL_NUM

def trace(self, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)

logging.Logger.trace = trace

env_log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, env_log_level, logging.INFO)

logging.basicConfig(
    level=log_level,
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- Paths ---
DATA_DIR = Path("/data")
BOOKS_DIR = Path("/books")
DB_FILE = DATA_DIR / "mapping_db.json"
STATE_FILE = DATA_DIR / "last_state.json"


class SyncManager:
    """
    Hardened Sync Manager with:
    - Process-safe JSON persistence via JsonDB
    - Furthest-wins leader election
    - Configurable anti-regression
    """
    
    def __init__(self):
        logger.info("=== Sync Manager Starting (Hardened) ===")
        
        # API Clients
        self.abs_client = ABSClient()
        self.kosync_client = KoSyncClient()
        self.storyteller_db = StorytellerDB()
        
        # Processing utilities
        self.transcriber = AudioTranscriber(DATA_DIR)
        self.ebook_parser = EbookParser(BOOKS_DIR)
        
        # Process-safe persistence
        self.db_handler = JsonDB(DB_FILE)
        self.state_handler = JsonDB(STATE_FILE)
        
        # Load initial state
        self.db = self.db_handler.load(default={"mappings": []})
        self.state = self.state_handler.load(default={})
        
        # Thresholds
        self.delta_abs_thresh = float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60))
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0
        self.delta_char_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_WORDS", 400)) * 5
        self.regression_threshold = float(os.getenv("SYNC_REGRESSION_THRESHOLD", 5)) / 100.0
        
        logger.info(f"Thresholds: ABS={self.delta_abs_thresh}s, KoSync={self.delta_kosync_thresh:.1%}, Regression={self.regression_threshold:.0%}")
        
        self.startup_checks()
        self.cleanup_stale_jobs()

    def startup_checks(self):
        logger.info("--- Connectivity Checks ---")
        abs_ok = self.abs_client.check_connection()
        kosync_ok = self.kosync_client.check_connection()
        storyteller_ok = self.storyteller_db.check_connection()
        
        if not abs_ok:
            logger.warning("⚠️ Audiobookshelf connection FAILED")
        if not kosync_ok:
            logger.warning("⚠️ KoSync connection FAILED")
        if not storyteller_ok:
            logger.warning("⚠️ Storyteller DB connection FAILED")

    def cleanup_stale_jobs(self):
        """Reset any crashed jobs to active."""
        changed = False
        for mapping in self.db.get('mappings', []):
            if mapping.get('status') == 'crashed':
                mapping['status'] = 'active'
                changed = True
                logger.info(f"Reset crashed: {mapping.get('abs_title', 'Unknown')}")
        if changed:
            self.db_handler.save(self.db)

    def _get_abs_title(self, item):
        """Extract title from ABS item."""
        title = item.get('media', {}).get('metadata', {}).get('title')
        if not title:
            title = item.get('name')
        if not title:
            title = item.get('title')
        return title or "Unknown"

    def get_text_from_storyteller_fragment(self, ebook_filename, href, fragment_id):
        """Extract text from EPUB using Storyteller's fragment ID."""
        if not href or not fragment_id:
            return None
        
        try:
            epub_path = None
            for f in BOOKS_DIR.rglob(ebook_filename):
                epub_path = f
                break
            
            if not epub_path:
                return None
            
            with ZipFile(epub_path, 'r') as zip_ref:
                internal_path = href
                if internal_path not in zip_ref.namelist():
                    matching = [f for f in zip_ref.namelist() if href in f]
                    if matching:
                        internal_path = matching[0]
                    else:
                        return None
                
                with zip_ref.open(internal_path) as f:
                    content = f.read()
                    parser = ET.HTMLParser(encoding='utf-8')
                    tree = ET.fromstring(content, parser)
                    elements = tree.xpath(f"//*[@id='{fragment_id}']")
                    
                    if elements:
                        text = "".join(elements[0].itertext()).strip()
                        return text
        except Exception as e:
            logger.error(f"Fragment extraction error: {e}")
        
        return None

    def _get_transcript_duration(self, transcript_path):
        """Get total duration from transcript file."""
        try:
            if not transcript_path or not Path(transcript_path).exists():
                return 0
            
            with open(transcript_path, 'r') as f:
                data = json.load(f)
            
            if isinstance(data, list) and data:
                return data[-1].get('end', 0)
            elif isinstance(data, dict):
                duration = data.get('duration', 0)
                if not duration and data.get('segments'):
                    duration = data['segments'][-1].get('end', 0)
                return duration
            return 0
        except Exception:
            return 0

    def _abs_to_percentage(self, abs_seconds, transcript_path):
        """Convert ABS timestamp to percentage."""
        duration = self._get_transcript_duration(transcript_path)
        if duration <= 0:
            return None  # Can't convert, don't use ABS as leader
        return min(max(abs_seconds / duration, 0.0), 1.0)

    def check_pending_jobs(self):
        """Process pending transcription jobs."""
        # Reload DB to catch web UI changes
        self.db = self.db_handler.load(default={"mappings": []})
        
        for mapping in self.db.get('mappings', []):
            status = mapping.get('status')
            abs_title = mapping.get('abs_title', 'Unknown')
            
            if status == 'pending':
                logger.info(f"[JOB] Starting: {abs_title}")
                mapping['status'] = 'processing'
                self.db_handler.save(self.db)
                
                try:
                    audio_files = self.abs_client.get_audio_files(mapping['abs_id'])
                    if not audio_files:
                        mapping['status'] = 'failed'
                        self.db_handler.save(self.db)
                        continue
                    
                    transcript_path = self.transcriber.process_audio(mapping['abs_id'], audio_files)
                    self.ebook_parser.extract_text_and_map(mapping['ebook_filename'])
                    
                    mapping['transcript_file'] = str(transcript_path)
                    mapping['status'] = 'active'
                    self.db_handler.save(self.db)
                    logger.info(f"[OK] {abs_title} is now active")
                    
                except Exception as e:
                    logger.error(f"[FAIL] {abs_title}: {e}")
                    mapping['status'] = 'failed_retry_later'
                    self.db_handler.save(self.db)
            
            elif status == 'pending_transcript':
                transcript_path = Path(mapping.get('transcript_file', ''))
                if transcript_path.exists():
                    logger.info(f"[OK] Transcript ready: {abs_title}")
                    mapping['status'] = 'active'
                    self.db_handler.save(self.db)

    def sync_cycle(self):
        """
        Main sync cycle with FURTHEST-WINS logic.
        
        1. Fetch progress from all platforms
        2. Convert to percentages for comparison
        3. Highest progress platform becomes leader
        4. Leader propagates to others
        5. Anti-regression blocks backwards movement
        """
        logger.debug("--- Sync Cycle ---")
        
        # Reload DB to catch web UI changes
        self.db = self.db_handler.load(default={"mappings": []})
        
        if not self.db.get('mappings'):
            return
        
        for mapping in self.db['mappings']:
            if mapping.get('status', 'active') != 'active':
                continue
            
            abs_id = mapping['abs_id']
            kosync_id = mapping['kosync_doc_id']
            transcript_path = mapping.get('transcript_file')
            ebook_filename = mapping['ebook_filename']
            abs_title = mapping.get('abs_title', 'Unknown')
            
            try:
                # === FETCH PROGRESS ===
                abs_seconds = self.abs_client.get_progress(abs_id) or 0.0
                kosync_pct = self.kosync_client.get_progress(kosync_id) or 0.0
                storyteller_pct, _ = self.storyteller_db.get_progress(ebook_filename)
                storyteller_pct = storyteller_pct or 0.0
                
                # Convert ABS to percentage
                abs_pct = self._abs_to_percentage(abs_seconds, transcript_path)
                
                logger.debug(f"[{abs_title}] ABS={abs_seconds:.0f}s ({abs_pct:.1%} if valid), KO={kosync_pct:.1%}, ST={storyteller_pct:.1%}")
                
            except Exception as e:
                logger.error(f"Fetch failed for {abs_title}: {e}")
                continue
            
            # === LOAD PREVIOUS STATE ===
            prev = self.state.get(abs_id, {})
            prev_abs_ts = prev.get('abs_ts', 0)
            prev_abs_pct = prev.get('abs_pct', 0)
            prev_kosync = prev.get('kosync_pct', 0)
            prev_storyteller = prev.get('storyteller_pct', 0)
            
            # === DETECT CHANGES ===
            abs_changed = abs(abs_seconds - prev_abs_ts) > self.delta_abs_thresh
            kosync_changed = abs(kosync_pct - prev_kosync) > self.delta_kosync_thresh
            storyteller_changed = abs(storyteller_pct - prev_storyteller) > self.delta_kosync_thresh
            
            # Character-based threshold for KoSync
            if not kosync_changed and kosync_pct != prev_kosync:
                char_delta = self.ebook_parser.get_character_delta(
                    ebook_filename, prev_kosync, kosync_pct
                )
                if char_delta and char_delta > self.delta_char_thresh:
                    kosync_changed = True
            
            # No significant changes
            if not abs_changed and not kosync_changed and not storyteller_changed:
                # Still update state for tiny changes to prevent loops
                if abs_seconds != prev_abs_ts or kosync_pct != prev_kosync or storyteller_pct != prev_storyteller:
                    self.state[abs_id] = {
                        'abs_ts': abs_seconds,
                        'abs_pct': abs_pct if abs_pct else prev_abs_pct,
                        'kosync_pct': kosync_pct,
                        'storyteller_pct': storyteller_pct,
                        'last_updated': prev.get('last_updated', 0)
                    }
                    self.state_handler.save(self.state)
                continue
            
            # === FURTHEST-WINS LEADER ELECTION ===
            # Build progress map (only include ABS if we can convert it)
            progress_map = {
                'KOSYNC': kosync_pct,
                'STORYTELLER': storyteller_pct
            }
            if abs_pct is not None:
                progress_map['ABS'] = abs_pct
            
            leader = max(progress_map, key=progress_map.get)
            leader_pct = progress_map[leader]
            
            changed_list = []
            if abs_changed:
                changed_list.append('ABS')
            if kosync_changed:
                changed_list.append('KOSYNC')
            if storyteller_changed:
                changed_list.append('STORYTELLER')
            
            logger.info(f"[{abs_title}] Changed: {changed_list}, Leader: {leader} ({leader_pct:.1%})")
            
            # === ANTI-REGRESSION CHECK ===
            prev_max = max(prev_abs_pct or 0, prev_kosync, prev_storyteller)
            
            if prev_max > 0 and (prev_max - leader_pct) > self.regression_threshold:
                logger.warning(f"  ⚠️ REGRESSION BLOCKED: {prev_max:.1%} → {leader_pct:.1%}")
                # Update state to prevent repeated warnings
                self.state[abs_id] = {
                    'abs_ts': abs_seconds,
                    'abs_pct': abs_pct if abs_pct else prev_abs_pct,
                    'kosync_pct': kosync_pct,
                    'storyteller_pct': storyteller_pct,
                    'last_updated': time.time()
                }
                self.state_handler.save(self.state)
                continue
            
            # === PROPAGATE FROM LEADER ===
            sync_success = False
            final_pct = leader_pct
            final_abs_ts = abs_seconds
            
            try:
                if leader == 'ABS':
                    target_text = self.transcriber.get_text_at_time(transcript_path, abs_seconds)
                    if target_text:
                        matched_pct, xpath, _ = self.ebook_parser.find_text_location(
                            ebook_filename, target_text, hint_percentage=abs_pct
                        )
                        if matched_pct is not None:
                            logger.info(f"  → ABS ({abs_pct:.1%}) syncing to {matched_pct:.1%}")
                            self.kosync_client.update_progress(kosync_id, matched_pct, xpath)
                            self.storyteller_db.update_progress(ebook_filename, matched_pct)
                            final_pct = matched_pct
                            sync_success = True
                
                elif leader == 'KOSYNC':
                    target_text = self.ebook_parser.get_text_at_percentage(ebook_filename, kosync_pct)
                    if target_text:
                        matched_time = self.transcriber.find_time_for_text(transcript_path, target_text)
                        if matched_time is not None:
                            logger.info(f"  → KoSync ({kosync_pct:.1%}) syncing to {matched_time:.0f}s")
                            self.abs_client.update_progress(abs_id, matched_time)
                            self.storyteller_db.update_progress(ebook_filename, kosync_pct)
                            final_abs_ts = matched_time
                            sync_success = True
                
                elif leader == 'STORYTELLER':
                    # Try fragment-based text first
                    st_pct, _, href, frag_id = self.storyteller_db.get_progress_with_fragment(ebook_filename)
                    target_text = None
                    
                    if frag_id:
                        target_text = self.get_text_from_storyteller_fragment(ebook_filename, href, frag_id)
                    
                    if not target_text or len(target_text) < 100:
                        target_text = self.ebook_parser.get_text_at_percentage(ebook_filename, storyteller_pct)
                    
                    if target_text:
                        matched_time = self.transcriber.find_time_for_text(transcript_path, target_text)
                        if matched_time is not None:
                            logger.info(f"  → Storyteller ({storyteller_pct:.1%}) syncing to {matched_time:.0f}s")
                            self.abs_client.update_progress(abs_id, matched_time)
                            _, xpath, _ = self.ebook_parser.find_text_location(ebook_filename, target_text)
                            self.kosync_client.update_progress(kosync_id, storyteller_pct, xpath)
                            final_abs_ts = matched_time
                            sync_success = True
                
            except Exception as e:
                logger.error(f"  Sync error: {e}")
            
            # === UPDATE STATE ===
            self.state[abs_id] = {
                'abs_ts': final_abs_ts,
                'abs_pct': self._abs_to_percentage(final_abs_ts, transcript_path) or abs_pct or 0,
                'kosync_pct': final_pct if leader != 'ABS' else kosync_pct,
                'storyteller_pct': final_pct if leader != 'ABS' else storyteller_pct,
                'last_updated': time.time()
            }
            self.state_handler.save(self.state)
            
            if sync_success:
                logger.info(f"  ✅ Sync complete")
            else:
                logger.warning(f"  ⚠️ Sync failed, state updated")

    def run_daemon(self):
        """Run the sync daemon."""
        period = int(os.getenv("SYNC_PERIOD_MINS", 5))
        schedule.every(period).minutes.do(self.sync_cycle)
        schedule.every(1).minutes.do(self.check_pending_jobs)
        
        logger.info(f"Daemon started. Sync every {period} mins.")
        
        # Initial sync
        self.sync_cycle()
        
        while True:
            schedule.run_pending()
            time.sleep(30)


if __name__ == "__main__":
    manager = SyncManager()
    manager.run_daemon()

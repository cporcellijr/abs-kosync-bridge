# [START FILE: abs-kosync-enhanced/main.py]
import os
import time
import json
import schedule
import logging
from pathlib import Path
from zipfile import ZipFile
import lxml.etree as ET

from json_db import JsonDB
from api_clients import ABSClient, KoSyncClient
from hardcover_client import HardcoverClient
from ebook_utils import EbookParser
from suggestion_manager import SuggestionManager

try:
    from storyteller_api import StorytellerDBWithAPI as StorytellerClient
except ImportError:
    from storyteller_db import StorytellerDB as StorytellerClient

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
        logger.info("=== Sync Manager Starting (Release 5.8 - Atomic Snapshot) ===")
        self.abs_client = ABSClient()
        self.kosync_client = KoSyncClient()
        self.hardcover_client = HardcoverClient()
        self.storyteller_db = StorytellerClient()
        self._transcriber = None
        self.ebook_parser = EbookParser(BOOKS_DIR)
        self.db_handler = JsonDB(DB_FILE)
        self.state_handler = JsonDB(STATE_FILE)
        self.db = self.db_handler.load(default={"mappings": []})
        self.state = self.state_handler.load(default={})
        self.suggestion_manager = SuggestionManager(DATA_DIR, self.ebook_parser, self.abs_client, self.storyteller_db)
        
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

    def cleanup_stale_jobs(self):
        changed = False
        for mapping in self.db.get('mappings', []):
            if mapping.get('status') == 'crashed':
                mapping['status'] = 'active'
                changed = True
        if changed: self.db_handler.save(self.db)

    def _get_abs_title(self, ab):
        """Extract title from audiobook item."""
        media = ab.get('media', {})
        metadata = media.get('metadata', {})
        return metadata.get('title') or ab.get('name', 'Unknown')

    def _automatch_hardcover(self, mapping):
        if not self.hardcover_client.token: return
        item = self.abs_client.get_item_details(mapping['abs_id'])
        if not item: return
        meta = item.get('media', {}).get('metadata', {})
        match = None
        if meta.get('isbn'): match = self.hardcover_client.search_by_isbn(meta.get('isbn'))
        if not match and meta.get('title'): match = self.hardcover_client.search_by_title_author(meta.get('title'), meta.get('authorName'))
        if match:
             mapping.update({'hardcover_book_id': match['book_id'], 'hardcover_edition_id': match.get('edition_id'), 'hardcover_pages': match.get('pages')})
             self.db_handler.save(self.db)
             self.hardcover_client.update_status(match['book_id'], 2, match.get('edition_id'))

    def _sync_to_hardcover(self, mapping, percentage):
        """
        Sync reading progress to Hardcover.app with proper date handling.
        
        - Sets started_at when first syncing progress
        - Sets finished_at when percentage > 99%
        - Updates status to "Read" (3) when finished
        """
        if not self.hardcover_client.token or not mapping.get('hardcover_book_id'): 
            return
        
        ub = self.hardcover_client.find_user_book(mapping['hardcover_book_id'])
        if ub:
            total_pages = mapping.get('hardcover_pages') or 0
            page_num = int(total_pages * percentage)
            is_finished = percentage > 0.99
            
            # Update progress (this now handles started_at and finished_at dates)
            self.hardcover_client.update_progress(
                ub['id'], 
                page_num, 
                edition_id=mapping.get('hardcover_edition_id'),
                is_finished=is_finished
            )
            
            # Also update status to "Read" when finished
            if is_finished:
                self.hardcover_client.update_status(
                    mapping['hardcover_book_id'], 
                    3,  # Status ID 3 = Read/Finished
                    mapping.get('hardcover_edition_id')
                )
                logger.info(f"📚 Hardcover: Marked '{mapping.get('abs_title', 'Unknown')}' as finished")

    def _abs_to_percentage(self, abs_seconds, transcript_path):
        try:
            with open(transcript_path, 'r') as f: 
                data = json.load(f)
                dur = data[-1]['end'] if isinstance(data, list) else data.get('duration', 0)
                return min(max(abs_seconds / dur, 0.0), 1.0) if dur > 0 else None
        except: return None

    def check_pending_jobs(self):
        self.db = self.db_handler.load(default={"mappings": []})
        for mapping in self.db.get('mappings', []):
            if mapping.get('status') == 'pending':
                logger.info(f"[JOB] Starting: {mapping.get('abs_title')}")
                mapping['status'] = 'processing'
                self.db_handler.save(self.db)
                try:
                    audio_files = self.abs_client.get_audio_files(mapping['abs_id'])
                    transcript_path = self.transcriber.process_audio(mapping['abs_id'], audio_files)
                    self.ebook_parser.extract_text_and_map(mapping['ebook_filename'])
                    mapping.update({'transcript_file': str(transcript_path), 'status': 'active'})
                    self.db_handler.save(self.db)
                    self._automatch_hardcover(mapping)
                except Exception as e:
                    logger.error(f"[FAIL] {mapping.get('abs_title')}: {e}")
                    mapping['status'] = 'failed_retry_later'
                    self.db_handler.save(self.db)

    def run_discovery(self):
        self.db = self.db_handler.load(default={"mappings": []})
        self.suggestion_manager.run_discovery_cycle([m['abs_id'] for m in self.db['mappings']])

    def get_text_from_storyteller_fragment(self, ebook_filename, href, fragment_id):
        if not href or not fragment_id: return None
        try:
            epub_path = next(BOOKS_DIR.rglob(ebook_filename), None)
            if not epub_path: return None
            
            with ZipFile(epub_path, 'r') as zip_ref:
                internal_path = href
                if internal_path not in zip_ref.namelist():
                    matching = [f for f in zip_ref.namelist() if href in f]
                    if matching: internal_path = matching[0]
                    else: return None
                
                with zip_ref.open(internal_path) as f:
                    content = f.read()
                    parser = ET.HTMLParser(encoding='utf-8')
                    tree = ET.fromstring(content, parser)
                    elements = tree.xpath(f"//*[@id='{fragment_id}']")
                    if elements: return "".join(elements[0].itertext()).strip()
        except Exception: return None

    def sync_cycle(self):
        self.db = self.db_handler.load(default={"mappings": []})
        self.state = self.state_handler.load(default={}) 
        
        active_books = [m for m in self.db.get('mappings', []) if m.get('status') == 'active']
        if active_books:
            logger.info(f"ðŸ”„ Sync cycle starting - {len(active_books)} active book(s)")
        
        for mapping in self.db.get('mappings', []):
            if mapping.get('status') != 'active': continue
            abs_id, ko_id, epub = mapping['abs_id'], mapping['kosync_doc_id'], mapping['ebook_filename']
            
            try:
                # 1. Fetch raw states (SNAPSHOT)
                # We fetch EVERYTHING from Storyteller now to ensure consistency.
                st_pct, st_ts, st_href, st_frag = self.storyteller_db.get_progress_with_fragment(epub)
                
                abs_ts = self.abs_client.get_progress(abs_id)
                ko_pct = self.kosync_client.get_progress(ko_id)
                
                # 2. [STRICT GUARD] If any service is offline (returns None), SKIP this book.
                
                if abs_ts is None:
                    logger.warning(f"âš ï¸  [{mapping.get('abs_title', 'Unknown')[:30]}] Skipped - ABS offline or unreachable")
                    continue

                abs_pct = self._abs_to_percentage(abs_ts, mapping.get('transcript_file'))
                
                if abs_ts > 0 and abs_pct is None:
                    logger.warning(f"âš ï¸  [{mapping.get('abs_title', 'Unknown')[:30]}] Skipped - Transcript invalid (ABS: {abs_ts:.0f}s)")
                    continue

                if ko_pct is None:
                    logger.warning(f"âš ï¸  [{mapping.get('abs_title', 'Unknown')[:30]}] Skipped - KOSync offline or unreachable")
                    continue

                # Treat Storyteller None as 0% (book just not started yet, not an error)
                if st_pct is None:
                    st_pct = 0.0
                
                prev = self.state.get(abs_id, {})
                vals = {'ABS': abs_pct or 0, 'KOSYNC': ko_pct, 'STORYTELLER': st_pct}
                
                changed = False
                if abs(vals['ABS'] - prev.get('abs_pct', 0)) > 0.01: changed = True
                if abs(vals['KOSYNC'] - prev.get('kosync_pct', 0)) > self.delta_kosync_thresh: changed = True
                if abs(vals['STORYTELLER'] - prev.get('storyteller_pct', 0)) > self.delta_kosync_thresh: changed = True
                
                if not changed:
                    if abs_id not in self.state: self.state[abs_id] = prev
                    self.state[abs_id]['last_updated'] = prev.get('last_updated', 0)
                    continue

                leader = max(vals, key=vals.get)
                
                # [SAFETY CHECK] 0% Leader Protection
                if vals[leader] == 0.0 and max(prev.get('abs_pct', 0), prev.get('kosync_pct', 0)) > 0.05:
                    logger.warning(f"ðŸ›¡ï¸  [{mapping.get('abs_title', 'Unknown')[:30]}] REGRESSION BLOCKED - {leader} tried to reset to 0% (was {max(prev.get('abs_pct', 0), prev.get('kosync_pct', 0)):.1%})")
                    continue

                logger.info(f"ðŸ“– [{mapping.get('abs_title', 'Unknown')[:30]}] {leader} leads at {vals[leader]:.1%} (ABS:{vals['ABS']:.1%} | KO:{vals['KOSYNC']:.1%} | ST:{vals['STORYTELLER']:.1%})")
                
                final_ts, final_pct = abs_ts, vals[leader]
                sync_success = False
                
                if leader == 'ABS':
                    txt = self.transcriber.get_text_at_time(mapping.get('transcript_file'), abs_ts)
                    if txt:
                        logger.debug(f"   ðŸ“ Got transcript text at {abs_ts:.0f}s ({len(txt)} chars)")
                        match_pct, rich_locator = self.ebook_parser.find_text_location(epub, txt, hint_percentage=abs_pct)
                        if match_pct:
                            self.kosync_client.update_progress(ko_id, match_pct, rich_locator.get('xpath'))
                            self.storyteller_db.update_progress(epub, match_pct, rich_locator)
                            final_pct = match_pct
                            sync_success = True
                            logger.info(f"   âœ… Synced to ebooks â†’ {match_pct:.1%}")
                        else:
                            logger.error(f"   âŒ Text match FAILED - couldn't locate in ebook")
                    else:
                        logger.error(f"   âŒ No transcript text at {abs_ts:.0f}s")

                elif leader == 'KOSYNC':
                    txt = self.ebook_parser.get_text_at_percentage(epub, ko_pct)
                    if txt:
                        logger.debug(f"   ðŸ“ Got ebook text at {ko_pct:.1%} ({len(txt)} chars)")
                        ts = self.transcriber.find_time_for_text(mapping.get('transcript_file'), txt, hint_percentage=ko_pct)
                        if ts:
                            self.abs_client.update_progress(abs_id, ts)
                            # Use KOReader's percentage directly for Storyteller, with rich locator
                            match_pct, rich_locator = self.ebook_parser.find_text_location(epub, txt, hint_percentage=ko_pct)
                            if match_pct:
                                self.storyteller_db.update_progress(epub, match_pct, rich_locator)
                                final_pct = match_pct
                            else:
                                # Fallback: use KOReader's percentage directly
                                self.storyteller_db.update_progress(epub, ko_pct, None)
                                final_pct = ko_pct
                            final_ts = ts
                            sync_success = True
                            logger.info(f"   âœ… Synced to ABS â†’ {ts:.0f}s, Storyteller â†’ {final_pct:.1%}")
                        else:
                            logger.error(f"   âŒ Timestamp match FAILED - couldn't find in transcript")
                    else:
                        logger.error(f"   âŒ No ebook text at {ko_pct:.1%}")

                elif leader == 'STORYTELLER':
                    # Use the SNAPSHOT data (st_href, st_frag) fetched at start of cycle.
                    # Do NOT query DB again, which might return stale/mismatched data.
                    txt = self.get_text_from_storyteller_fragment(epub, st_href, st_frag) if st_frag else None
                    if not txt: 
                        txt = self.ebook_parser.get_text_at_percentage(epub, st_pct)
                        logger.debug(f"   ðŸ“ Using ebook text at {st_pct:.1%} (no fragment data)")
                    else:
                        logger.debug(f"   ðŸ“ Using fragment text from {st_href}#{st_frag} ({len(txt)} chars)")
                    
                    if txt:
                        ts = self.transcriber.find_time_for_text(mapping.get('transcript_file'), txt, hint_percentage=st_pct)
                        if ts:
                            self.abs_client.update_progress(abs_id, ts)
                            match_pct, rich_locator = self.ebook_parser.find_text_location(epub, txt, hint_percentage=st_pct)
                            if match_pct:
                                self.kosync_client.update_progress(ko_id, match_pct, rich_locator.get('xpath'))
                                final_pct = match_pct
                            else:
                                # Fallback: use Storyteller's percentage
                                self.kosync_client.update_progress(ko_id, st_pct, None)
                                final_pct = st_pct
                            final_ts = ts
                            sync_success = True
                            logger.info(f"   âœ… Synced to ABS â†’ {ts:.0f}s, KOReader â†’ {final_pct:.1%}")
                        else:
                            logger.error(f"   âŒ Timestamp match FAILED - couldn't find in transcript")
                    else:
                        logger.error(f"   âŒ No ebook text available")
                
                if sync_success and final_pct > 0.01:
                    if not mapping.get('hardcover_book_id'): self._automatch_hardcover(mapping)
                    if mapping.get('hardcover_book_id'): self._sync_to_hardcover(mapping, final_pct)

                self.state[abs_id] = {
                    'abs_ts': final_ts, 
                    'abs_pct': self._abs_to_percentage(final_ts, mapping.get('transcript_file')) or 0, 
                    'kosync_pct': final_pct, 
                    'storyteller_pct': final_pct, 
                    'last_updated': time.time()
                }
                self.state_handler.save(self.state)
                
            except Exception as e:
                logger.error(f"Sync error: {e}")

    def run_daemon(self):
        schedule.every(int(os.getenv("SYNC_PERIOD_MINS", 5))).minutes.do(self.sync_cycle)
        schedule.every(1).minutes.do(self.check_pending_jobs)
        schedule.every(15).minutes.do(self.run_discovery)
        logger.info("Daemon started.")
        self.sync_cycle()
        while True:
            schedule.run_pending()
            time.sleep(30)

if __name__ == "__main__":
    SyncManager().run_daemon()
# [END FILE]
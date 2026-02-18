"""
Alignment Service.
Handles the core logic for aligning ebook text with audio transcriptions
and storing the results in the database.
"""

import json
import logging
import re
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from src.db.models import BookAlignment
from src.utils.polisher import Polisher
from src.utils.logging_utils import time_execution

logger = logging.getLogger(__name__)

class AlignmentService:
    def __init__(self, database_service, polisher: Polisher):
        self.database_service = database_service
        self.polisher = polisher

    @time_execution
    def align_and_store(self, abs_id: str, raw_segments: List[Dict], ebook_text: str, spine_chapters: List[Dict] = None):
        """
        Main entry point for "Unified Alignment".
        
        Steps:
        1. Validate Structure: Ensure we aren't trying to align mismatched content.
           (e.g., if spine_chapters provided, check roughly if segment count matches or text length matches).
        2. Normalize: Use Polisher to clean both raw transcript and ebook text.
        3. Anchor: Run N-Gram alignment to map characters to timestamps.
        4. Rebuild: Fix fragmented sentences in transcript using ebook text as a guide.
        5. Store: Save ONLY the mapping and essential metadata to DB.
        """
        logger.info(f"AlignmentService: Processing {abs_id} (Text: {len(ebook_text)} chars, Segments: {len(raw_segments)})")

        # 1. Validation (Spine Check)
        # Note: This is soft validation. If lengths assume vastly different sizes, warn.
        # Implementation of full spine verification requires mapping chapters to segments.
        # For now, we trust the inputs but log warnings.
        ebook_len = len(ebook_text)
        # Estimate audio text length
        audio_text_rough = " ".join([s['text'] for s in raw_segments])
        audio_len = len(audio_text_rough)
        
        ratio = audio_len / ebook_len if ebook_len > 0 else 0
        if ratio < 0.5 or ratio > 1.5:
             logger.warning(f"⚠️ Alignment Size Mismatch: Audio text is {ratio:.2%} of Ebook text size.")

        # 2. Normalize & Rebuild
        # Fix fragmented sentences (Mr. Smith case)
        # We pass ebook_text to help (though rebuild_fragmented_sentences uses simple heuristics currently)
        rebuilt_segments = self.polisher.rebuild_fragmented_sentences(raw_segments, ebook_text)
        logger.info(f"   Rebuilt segments: {len(raw_segments)} -> {len(rebuilt_segments)}")

        # 3. Anchored Alignment
        alignment_map = self._generate_alignment_map(rebuilt_segments, ebook_text)
        
        if not alignment_map:
            logger.error("   ❌ Failed to generate alignment map.")
            return False

        # 4. Store to Database
        self._save_alignment(abs_id, alignment_map)
        return True

    def get_time_for_text(self, abs_id: str, query_text: str, char_offset_hint: int = None) -> Optional[float]:
        """
        Precise time lookup.
        If char_offset_hint is provided (from ebook reader), use it directly with the map.
        Otherwise, fuzzy search the text to find offset, then use map.
        """
        # 1. Fetch Alignment Map
        alignment = self._get_alignment(abs_id)
        if not alignment:
            return None
        
        map_points = alignment
        
        # 2. Resolve offset
        target_offset = char_offset_hint
        
        if target_offset is None:
            # Note: For now, KOSync always provides an offset or we calculate it.
            return None

        # 3. Interpolate Timestamp
        # Binary search
        left = 0
        right = len(map_points) - 1
        
        # Points are [{'char': x, 'ts': y}, ...]
        # Find interval [p1, p2] where p1.char <= target <= p2.char
        
        if target_offset < map_points[0]['char']:
            return map_points[0]['ts']
        if target_offset > map_points[-1]['char']:
            return map_points[-1]['ts']

        # Manual binary search to find floor
        floor_idx = 0
        while left <= right:
            mid = (left + right) // 2
            if map_points[mid]['char'] <= target_offset:
                floor_idx = mid
                left = mid + 1
            else:
                right = mid - 1
        
        p1 = map_points[floor_idx]
        
        # Ceiling is next point
        if floor_idx + 1 < len(map_points):
            p2 = map_points[floor_idx + 1]
        else:
            return p1['ts']

        # Linear Interpolation
        char_span = p2['char'] - p1['char']
        time_span = p2['ts'] - p1['ts']
        
        if char_span == 0: return p1['ts']
        
        ratio = (target_offset - p1['char']) / char_span
        estimated_time = p1['ts'] + (time_span * ratio)
        
        return float(estimated_time)

    def get_char_for_time(self, abs_id: str, timestamp: float) -> Optional[int]:
        """
        Reverse lookup: Find character offset for a given timestamp.
        """
        # 1. Fetch Alignment Map
        alignment = self._get_alignment(abs_id)
        if not alignment:
            return None
        
        map_points = alignment
        target_ts = timestamp
        
        # 2. Binary search for interval
        left = 0
        right = len(map_points) - 1
        
        if target_ts <= map_points[0]['ts']:
            return int(map_points[0]['char'])
        if target_ts >= map_points[-1]['ts']:
            return int(map_points[-1]['char'])
            
        floor_idx = 0
        while left <= right:
            mid = (left + right) // 2
            if map_points[mid]['ts'] <= target_ts:
                floor_idx = mid
                left = mid + 1
            else:
                right = mid - 1
        
        p1 = map_points[floor_idx]
        if floor_idx + 1 < len(map_points):
            p2 = map_points[floor_idx + 1]
        else:
            return int(p1['char'])
            
        # 3. Interpolate
        time_span = p2['ts'] - p1['ts']
        char_span = p2['char'] - p1['char']
        
        if time_span == 0: return int(p1['char'])
        
        ratio = (target_ts - p1['ts']) / time_span
        estimated_char = p1['char'] + (char_span * ratio)
        
        return int(estimated_char)

    def _generate_alignment_map(self, segments: List[Dict], full_text: str) -> List[Dict]:
        """
        Core Anchored Alignment Algorithm (Two-Pass).
        Pass 1: High confidence (N=12) global search.
        Pass 2: Backfill start gap (N=6) if first anchor is late.
        """
        # 1. Tokenize Transcript
        transcript_words = []
        for seg in segments:
            raw_words = seg['text'].split()
            if not raw_words: continue
            
            duration = seg['end'] - seg['start']
            per_word = duration / len(raw_words)
            
            for i, w in enumerate(raw_words):
                norm = self.polisher.normalize(w)
                if not norm: continue
                transcript_words.append({
                    "word": norm,
                    "ts": seg['start'] + (i * per_word),
                    "orig_index": len(transcript_words) # Keep track for slicing
                })

        # 2. Tokenize Book
        book_words = []
        for match in re.finditer(r'\S+', full_text):
            raw_w = match.group()
            norm = self.polisher.normalize(raw_w)
            if not norm: continue
            book_words.append({
                "word": norm,
                "char": match.start(),
                "orig_index": len(book_words)
            })

        if not transcript_words or not book_words:
            return []

        # --- Helper for N-Gram Logic ---
        def _find_anchors(t_tokens, b_tokens, n_size):
            # Build N-Grams
            def build_ngrams(items, is_book=False):
                grams = {}
                for i in range(len(items) - n_size + 1):
                    keys = [x['word'] for x in items[i:i+n_size]]
                    key = "_".join(keys)
                    if key not in grams: grams[key] = []
                    # Store entire object to retrieve ts/char/index
                    grams[key].append(items[i])
                return grams

            t_grams = build_ngrams(t_tokens, False)
            b_grams = build_ngrams(b_tokens, True)

            found = []
            for key, t_list in t_grams.items():
                if len(t_list) == 1: # Unique in transcript slice
                    if key in b_grams and len(b_grams[key]) == 1: # Unique in book slice
                        # Safe access using indices
                        b_item = b_grams[key][0]
                        t_item = t_list[0]
                        found.append({
                            "ts": t_item['ts'],
                            "char": b_item['char'],
                            "t_idx": t_item['orig_index'],
                            "b_idx": b_item['orig_index']
                        })
            return found

        # 3. PASS 1: Global Search (N=12)
        anchors = _find_anchors(transcript_words, book_words, n_size=12)
        
        # Sort by character position
        anchors.sort(key=lambda x: x['char'])
        
        # Filter Monotonic (Global)
        valid_anchors = []
        if anchors:
            valid_anchors.append(anchors[0])
            for a in anchors[1:]:
                if a['ts'] > valid_anchors[-1]['ts']:
                    valid_anchors.append(a)

        # 4. PASS 2: Backfill Start (N=6) "Work Backwards"
        # If the first anchor is significantly into the book, try to recover the intro.
        # Threshold: First anchor is > 1000 chars in AND > 30 seconds in
        if valid_anchors and valid_anchors[0]['char'] > 1000 and valid_anchors[0]['ts'] > 30.0:
            first = valid_anchors[0]
            logger.info(f"   🔄 Late start detected (Char: {first['char']}, TS: {first['ts']:.1f}s) — Attempting backfill")

            # Slice the data: Everything BEFORE the first anchor
            # We use the indices we stored during tokenization
            t_slice = transcript_words[:first['t_idx']]
            b_slice = book_words[:first['b_idx']]

            if t_slice and b_slice:
                # Run with reduced N-Gram (N=6)
                # Lower N is risky globally, but safe in this small constrained window
                early_anchors = _find_anchors(t_slice, b_slice, n_size=6)
                
                # Filter Early Anchors (Must be monotonic with themselves)
                early_anchors.sort(key=lambda x: x['char'])
                valid_early = []
                if early_anchors:
                    valid_early.append(early_anchors[0])
                    for a in early_anchors[1:]:
                        if a['ts'] > valid_early[-1]['ts']:
                            valid_early.append(a)
                
                if valid_early:
                    logger.info(f"   ✅ Backfill success: Recovered {len(valid_early)} early anchors.")
                    # Prepend to main list
                    valid_anchors = valid_early + valid_anchors

        # 5. Build Final Map
        final_map = []
        if not valid_anchors:
            return []

        # Force 0,0 if still missing (Linear Interpolation fallback)
        if valid_anchors[0]['char'] > 0:
            final_map.append({"char": 0, "ts": 0.0})
            
        final_map.extend(valid_anchors)
        
        # Force End
        last = valid_anchors[-1]
        if last['char'] < len(full_text):
            # Safe check for segments
            end_ts = segments[-1]['end'] if segments else last['ts']
            final_map.append({"char": len(full_text), "ts": end_ts})

        logger.info(f"   ⚓ Anchored Alignment: Found {len(valid_anchors)} anchors (Total).")
        return final_map

    def _save_alignment(self, abs_id: str, alignment_map: List[Dict]):
        """Upsert alignment to SQLite."""
        with self.database_service.get_session() as session:
            json_blob = json.dumps(alignment_map)
            
            # Check exist
            existing = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
            if existing:
                existing.alignment_map_json = json_blob
                existing.last_updated = datetime.utcnow()
            else:
                new_align = BookAlignment(abs_id=abs_id, alignment_map_json=json_blob)
                session.add(new_align)
            
            # Context manager handles commit
            logger.info(f"   💾 Saved alignment for {abs_id} to DB.")
 
    def _get_alignment(self, abs_id: str) -> Optional[List[Dict]]:
        with self.database_service.get_session() as session:
            entry = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
            if entry:
                return json.loads(entry.alignment_map_json)
            return None
    def get_book_duration(self, abs_id: str) -> Optional[float]:
        """Get the total duration of the book from its alignment map."""
        alignment = self._get_alignment(abs_id)
        if alignment and len(alignment) > 0:
            # The last point in the alignment map should have the max timestamp
            return float(alignment[-1]['ts'])
        return None

"""
Storyteller transcript reader.

Reads BookBridge storyteller manifests and chapter JSON files lazily.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


class StorytellerTranscript:
    """Lazy, chapter-aware reader for storyteller forced-alignment transcripts."""

    def __init__(self, manifest_path: str | Path, cache_capacity: int = 3):
        self.manifest_path = Path(manifest_path)
        self.base_dir = self.manifest_path.parent
        self._cache_capacity = max(1, int(cache_capacity))
        self._chapter_cache: OrderedDict[int, Dict] = OrderedDict()

        with open(self.manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        if not isinstance(manifest, dict) or manifest.get("format") != "storyteller_manifest":
            raise ValueError(f"Invalid storyteller manifest: {self.manifest_path}")

        chapters = list(manifest.get("chapters") or [])
        chapters.sort(key=lambda ch: int(ch.get("index", 0)))
        self._chapters = chapters
        self._duration = float(manifest.get("duration", 0.0) or 0.0)
        self._chapter_starts = [float(ch.get("start", 0.0) or 0.0) for ch in self._chapters]

    @property
    def chapters(self) -> List[Dict]:
        return self._chapters

    def chapter_count(self) -> int:
        return len(self._chapters)

    def get_duration(self) -> float:
        """Return last chapter's local end time (chapter-relative)."""
        if not self._chapters:
            return 0.0
        chapter = self._load_chapter(len(self._chapters) - 1)
        timeline = chapter["word_timeline"]
        if not timeline:
            return 0.0
        return float(timeline[-1].get("endTime", 0.0) or 0.0)

    def get_global_duration(self) -> float:
        if self._duration > 0:
            return self._duration
        if not self._chapters:
            return 0.0
        last_idx = len(self._chapters) - 1
        last_meta = self._chapters[last_idx]
        return float(last_meta.get("start", 0.0) or 0.0) + self.get_duration()

    def get_text_at_time(self, timestamp: float, chapter_index: Optional[int] = None) -> Optional[str]:
        """Return ~800 chars of context around a chapter-local or global timestamp."""
        if chapter_index is None:
            chapter_index, local_ts = self._resolve_chapter_for_global_timestamp(float(timestamp))
        else:
            local_ts = float(timestamp)

        chapter = self._load_chapter(chapter_index)
        idx = self._search_floor(chapter["start_times"], local_ts)
        if idx is None:
            return None
        offset = chapter["start_offsets"][idx]
        return self._context_from_offset(chapter["transcript"], offset)

    def get_text_at_character_offset(self, offset: int, chapter_index: int) -> Optional[str]:
        chapter = self._load_chapter(chapter_index)
        idx = self._search_floor(chapter["start_offsets"], int(offset))
        if idx is None:
            return None
        return self._context_from_offset(chapter["transcript"], chapter["start_offsets"][idx])

    def timestamp_to_char_offset(self, timestamp: float, chapter_index: Optional[int] = None) -> Optional[int]:
        if chapter_index is None:
            chapter_index, local_ts = self._resolve_chapter_for_global_timestamp(float(timestamp))
        else:
            local_ts = float(timestamp)

        chapter = self._load_chapter(chapter_index)
        idx = self._search_floor(chapter["start_times"], local_ts)
        if idx is None:
            return None
        return int(chapter["start_offsets"][idx])

    def char_offset_to_timestamp(self, offset: int, chapter_index: int) -> Optional[float]:
        chapter = self._load_chapter(chapter_index)
        idx = self._search_floor(chapter["start_offsets"], int(offset))
        if idx is None:
            return None
        return float(chapter["start_times"][idx])

    def iter_alignment_points(self) -> Iterable[Dict]:
        """
        Yield chapter-aware alignment entries.

        Output fields:
        - chapter: ABS chapter index
        - char: chapter-local UTF-16 offset
        - local_ts: chapter-local timestamp
        - ts: global ABS timestamp
        - global_char: cumulative transcript offset across chapters (adapter for existing lookups)
        """
        global_char_base = 0
        for chapter_index, meta in enumerate(self._chapters):
            chapter = self._load_chapter(chapter_index)
            chapter_start = float(meta.get("start", 0.0) or 0.0)
            for word in chapter["word_timeline"]:
                local_ts = float(word.get("startTime", 0.0) or 0.0)
                local_char = int(word.get("startOffsetUtf16", 0) or 0)
                yield {
                    "chapter": chapter_index,
                    "char": local_char,
                    "local_ts": local_ts,
                    "ts": chapter_start + local_ts,
                    "global_char": global_char_base + local_char,
                }
            global_char_base += len(chapter["transcript"]) + 1

    def _resolve_chapter_for_global_timestamp(self, timestamp: float) -> Tuple[int, float]:
        if not self._chapters:
            return 0, float(timestamp)

        # Prefer strict chapter boundaries if present.
        for idx, chapter in enumerate(self._chapters):
            start = float(chapter.get("start", 0.0) or 0.0)
            end = float(chapter.get("end", start) or start)
            if start <= timestamp <= end:
                return idx, max(0.0, timestamp - start)

        idx = bisect_right(self._chapter_starts, timestamp) - 1
        if idx < 0:
            idx = 0
        if idx >= len(self._chapters):
            idx = len(self._chapters) - 1
        chapter_start = float(self._chapters[idx].get("start", 0.0) or 0.0)
        return idx, max(0.0, timestamp - chapter_start)

    def _load_chapter(self, chapter_index: int) -> Dict:
        chapter_index = int(chapter_index)
        if chapter_index < 0 or chapter_index >= len(self._chapters):
            raise IndexError(f"Storyteller chapter out of range: {chapter_index}")

        if chapter_index in self._chapter_cache:
            self._chapter_cache.move_to_end(chapter_index)
            return self._chapter_cache[chapter_index]

        chapter_meta = self._chapters[chapter_index]
        chapter_file = self.base_dir / str(chapter_meta.get("file", ""))
        with open(chapter_file, "r", encoding="utf-8") as f:
            raw = json.load(f)

        if not isinstance(raw, dict) or "wordTimeline" not in raw:
            raise ValueError(f"Invalid storyteller chapter format: {chapter_file}")

        transcript = raw.get("transcript", "")
        timeline = list(raw.get("wordTimeline") or [])
        timeline.sort(key=lambda x: float(x.get("startTime", 0.0) or 0.0))
        start_times = [float(w.get("startTime", 0.0) or 0.0) for w in timeline]
        start_offsets = [int(w.get("startOffsetUtf16", 0) or 0) for w in timeline]

        chapter_data = {
            "transcript": transcript,
            "word_timeline": timeline,
            "start_times": start_times,
            "start_offsets": start_offsets,
        }

        self._chapter_cache[chapter_index] = chapter_data
        self._chapter_cache.move_to_end(chapter_index)
        if len(self._chapter_cache) > self._cache_capacity:
            self._chapter_cache.popitem(last=False)
        return chapter_data

    @staticmethod
    def _search_floor(values: List[float] | List[int], target: float | int) -> Optional[int]:
        if not values:
            return None
        idx = bisect_right(values, target) - 1
        if idx < 0:
            return 0
        if idx >= len(values):
            return len(values) - 1
        return idx

    @staticmethod
    def _context_from_offset(transcript_text: str, offset: int, target_len: int = 800) -> str:
        if not transcript_text:
            return ""
        half = target_len // 2
        start = max(0, int(offset) - half)
        end = min(len(transcript_text), start + target_len)
        if end - start < target_len and start > 0:
            start = max(0, end - target_len)
        return " ".join(transcript_text[start:end].split())

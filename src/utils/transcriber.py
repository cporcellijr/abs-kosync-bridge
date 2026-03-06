# [START FILE: abs-kosync-enhanced/transcriber.py]
"""
Audio Transcriber for abs-kosync-enhanced

UPDATED VERSION with:
- stalign readaloud EPUB generation fallback
- LRU transcript cache
- Long file splitting
- Configurable fuzzy match threshold
- Context gathering for text matching
- Dependency Injection for SmilExtractor
"""

import json
import logging
import os
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Optional
import re
from bisect import bisect_right
from collections import OrderedDict

from src.utils.logging_utils import sanitize_log_data, time_execution
from src.utils.polisher import Polisher
from src.utils.storyteller_transcript import StorytellerTranscript
# We keep the import for type hinting, but we don't instantiate it directly anymore

logger = logging.getLogger(__name__)

class AudioTranscriber:
    # [UPDATED] Accepted smil_extractor and polisher as arguments
    def __init__(self, data_dir, smil_extractor, polisher: Polisher):
        self.data_dir = data_dir

        self.stalign_path = os.environ.get("STALIGN_PATH", "/usr/local/bin/stalign")
        self.stalign_engine = os.environ.get("STALIGN_ENGINE", "whisper.cpp")
        self.stalign_whisper_model = os.environ.get("STALIGN_WHISPER_MODEL", "tiny.en")
        self.stalign_granularity = os.environ.get("STALIGN_GRANULARITY", "sentence")
        self.stalign_timeout_mins = int(os.environ.get("STALIGN_TIMEOUT_MINS", "60"))

        self._transcript_cache = OrderedDict()
        self._cache_capacity = 3

        # Unified threshold logic
        self.match_threshold = int(os.environ.get("TRANSCRIPT_MATCH_THRESHOLD", os.environ.get("FUZZY_MATCH_THRESHOLD", 80)))

        # [UPDATED] Use the injected instances
        self.smil_extractor = smil_extractor
        self.polisher = polisher

        logger.info(f"🛠️ stalign configured: path={self.stalign_path}, engine={self.stalign_engine}, timeout={self.stalign_timeout_mins}m")


    def validate_smil(self, smil_segments: list, ebook_text: str) -> tuple[bool, float]:
        """
        Robustly validate SMIL alignment using text similarity.
        
        1. Overlap Check: Basic sanity check.
        2. Content Match: Normalize both texts and calculate similarity ratio.
           This allows SMIL to be slightly off but still accepted if it largely matches.
        
        Returns:
            (is_valid, score) - score is overlap_ratio (if failed overlap) or match_percentage (if passed overlap)
        """
        if not smil_segments or len(smil_segments) < 2:
             return True, 1.0

        # 1. Overlap Check (Basic) - Allow up to 15% overlap noise
        overlap_count = 0
        for i in range(1, len(smil_segments)):
            if smil_segments[i]['start'] < smil_segments[i-1]['end']:
                overlap_count += 1
        
        overlap_ratio = overlap_count / len(smil_segments)
        if overlap_ratio > 0.15: # 15% threshold
            logger.warning(f"⚠️ SMIL contains explicit overlaps ({overlap_ratio:.1%}) — Might be invalid")
            # Don't fail just on overlap if text match is perfect (e.g. concurrent audio layers in SMIL)
            # But usually high overlap means bad SMIL.
        
        # 2. Content Validation (The "Swift Rejects" Fix)
        # We need to see if the SMIL text actually plausibly exists in the ebook text.
        # This prevents accepting "Page 1", "Page 2" type SMILs that don't match audio content.
        
        # Construct full SMIL text
        smil_text_raw = " ".join([s['text'] for s in smil_segments])
        smil_norm = self.polisher.normalize(smil_text_raw)
        
        # Normalize a chunk of ebook text (first 50k chars to save time, or fully if small)
        # Ideally, we used the full extracted text passed in.
        ebook_norm = self.polisher.normalize(ebook_text[:max(len(ebook_text), len(smil_text_raw)*2)])
        
        if not smil_norm:
            return False, 0.0
            
        # Using simple token overlap ratio for speed
        # Levenshtein on huge strings is slow.
        smil_tokens = set(smil_norm.split())
        ebook_tokens = set(ebook_norm.split())
        
        common = smil_tokens.intersection(ebook_tokens)
        if not smil_tokens: return False, 0.0
        
        match_ratio = len(common) / len(smil_tokens)
        
        # Acceptance Criteria: 
        # Must have significant text overlap (proving it aligns to THIS book)
        # Threshold is configurable via SMIL_VALIDATION_THRESHOLD (default 60%)
        smil_threshold = float(os.getenv("SMIL_VALIDATION_THRESHOLD", "60")) / 100.0
        logger.info(f"   📊 SMIL Validation: Overlap={overlap_ratio:.1%}, Token Match={match_ratio:.1%} (threshold={smil_threshold:.0%})")
        if match_ratio < smil_threshold:
             return False, match_ratio

        return True, match_ratio

    def transcribe_from_smil(self, abs_id: str, epub_path: Path, abs_chapters: list, full_book_text: str = None, progress_callback=None) -> Optional[list]:
        """
        Attempts to extract a transcript directly from the EPUB's SMIL overlay data.
        Returns RAW SEGMENTS (list of dicts) if successful, None otherwise.
        """
        if progress_callback: progress_callback(0.0)

        if not self.smil_extractor.has_media_overlays(str(epub_path)):
            return None

        logger.info(f"⚡ Fast-Path: Extracting transcript from SMIL for {abs_id}...")

        try:
            transcript = self.smil_extractor.extract_transcript(str(epub_path), abs_chapters)
            if not transcript:
                return None

            # [FAILSAFE] Check Duration / Coverage
            # If the SMIL transcript is significantly shorter than the audiobook, reject it.
            if abs_chapters and len(abs_chapters) > 0:
                expected_duration = float(abs_chapters[-1].get('end', 0))
                if expected_duration > 0:
                    transcript_duration = transcript[-1]['end']
                    coverage = transcript_duration / expected_duration
                    
                    # Reject if coverage is less than 85%
                    if coverage < 0.85:
                        logger.warning(f"⚠️ SMIL REJECTED: Coverage too low ({coverage:.1%}). Expected {expected_duration:.0f}s, got {transcript_duration:.0f}s — Falling back to transcriber")
                        return None

            # [NEW] Validate transcript against BOOK TEXT
            # We require full_book_text for this validation.
            if full_book_text:
                is_valid, score = self.validate_smil(transcript, full_book_text)
                
                if not is_valid:
                    logger.warning(f"⚠️ SMIL validation failed: Match score {score:.1%} too low")
                    logger.info(f"🔄 Falling back to Whisper transcription for {abs_id}")
                    return None
                else:
                    logger.info(f"✅ SMIL Validated (Match: {score:.1%})")
            else:
                logger.warning("⚠️ Skipping detailed SMIL validation (no ebook text provided)")

            logger.info(f"✅ SMIL Extraction complete: {len(transcript)} segments")
            return transcript # Return raw data!
        except Exception as e:
            logger.error(f"❌ Failed to extract SMIL transcript: {e}")
            return None

    def _get_cached_transcript(self, path):
        """Load transcript with LRU caching."""
        path_str = str(path)
        if path_str in self._transcript_cache:
            self._transcript_cache.move_to_end(path_str)
            return self._transcript_cache[path_str]

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data_format = self._detect_transcript_format(data)
            if data_format == "storyteller_manifest":
                loaded = StorytellerTranscript(path, cache_capacity=self._cache_capacity)
            else:
                loaded = data
            self._transcript_cache[path_str] = loaded
            self._transcript_cache.move_to_end(path_str)
            if len(self._transcript_cache) > self._cache_capacity:
                self._transcript_cache.popitem(last=False)
            return loaded
        except Exception as e:
            logger.error(f"❌ Error loading transcript '{path}': {e}")
            return None

    def _detect_transcript_format(self, data):
        """Detect storyteller-rich vs legacy segment transcript formats."""
        if isinstance(data, dict) and data.get("format") == "storyteller_manifest":
            return "storyteller_manifest"
        if isinstance(data, dict) and (
            isinstance(data.get("wordTimeline"), list) or isinstance(data.get("timeline"), list)
        ):
            return "storyteller_word_timeline"
        if isinstance(data, list) and data and isinstance(data[0], dict) and 'start' in data[0]:
            return "segment_list"
        return "unknown"

    @staticmethod
    def _get_storyteller_timeline(data):
        if not isinstance(data, dict):
            return []
        timeline = data.get("wordTimeline")
        if isinstance(timeline, list):
            return timeline
        timeline = data.get("timeline")
        if isinstance(timeline, list):
            return timeline
        return []

    @staticmethod
    def _storyteller_floor(values, target):
        if not values:
            return None
        idx = bisect_right(values, target) - 1
        if idx < 0:
            return 0
        if idx >= len(values):
            return len(values) - 1
        return idx

    @staticmethod
    def _storyteller_context(transcript_text, offset, target_len=800):
        if not transcript_text:
            return ""
        half = target_len // 2
        start = max(0, int(offset) - half)
        end = min(len(transcript_text), start + target_len)
        if end - start < target_len and start > 0:
            start = max(0, end - target_len)
        return re.sub(r'\s+', ' ', transcript_text[start:end]).strip()

    def _storyteller_text_at_time(self, data, timestamp):
        timeline = self._get_storyteller_timeline(data)
        if not timeline:
            return None
        start_times = [float(w.get("startTime", 0.0) or 0.0) for w in timeline]
        idx = self._storyteller_floor(start_times, float(timestamp))
        if idx is None:
            return None
        offset = int(timeline[idx].get("startOffsetUtf16", 0) or 0)
        return self._storyteller_context(data.get("transcript", ""), offset)

    def _storyteller_time_for_offset(self, data, offset):
        timeline = self._get_storyteller_timeline(data)
        if not timeline:
            return None
        start_offsets = [int(w.get("startOffsetUtf16", 0) or 0) for w in timeline]
        idx = self._storyteller_floor(start_offsets, int(offset))
        if idx is None:
            return None
        return float(timeline[idx].get("startTime", 0.0) or 0.0)

    def _clean_text(self, text):
        """Aggressive text cleaner to boost fuzzy match scores."""
        if not text:
            return ""
        return re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _add_engine_arg(cmd: list[str], flag: str, env_name: str, env: dict[str, str]):
        value = env.get(env_name)
        if value:
            cmd.extend([flag, value])

    def _build_stalign_command(self, epub_path: Path, audiobook_dir: Path, output_dir: Path, engine_config: Optional[dict] = None) -> list[str]:
        env = dict(os.environ)
        if engine_config:
            env.update({k: str(v) for k, v in engine_config.items() if v is not None})

        engine = env.get("STALIGN_ENGINE", self.stalign_engine)
        model = env.get("STALIGN_WHISPER_MODEL", self.stalign_whisper_model)

        cmd = [
            self.stalign_path,
            "--audiobook", str(audiobook_dir),
            "--epub", str(epub_path),
            "--output", str(output_dir),
            "--engine", engine,
            "--log-level", "info",
            "--no-progress",
            "--granularity", "sentence",
        ]

        if engine == "whisper.cpp":
            cmd.extend(["--model", model or "tiny.en"])
        elif engine == "openai-cloud":
            self._add_engine_arg(cmd, "--openai-api-key", "STALIGN_OPENAI_API_KEY", env)
            self._add_engine_arg(cmd, "--openai-base-url", "STALIGN_OPENAI_BASE_URL", env)
            self._add_engine_arg(cmd, "--openai-model", "STALIGN_OPENAI_MODEL", env)
        elif engine == "deepgram":
            self._add_engine_arg(cmd, "--deepgram-api-key", "STALIGN_DEEPGRAM_API_KEY", env)
            self._add_engine_arg(cmd, "--deepgram-model", "STALIGN_DEEPGRAM_MODEL", env)
        elif engine == "whisper-server":
            self._add_engine_arg(cmd, "--whisper-server-url", "STALIGN_WHISPER_SERVER_URL", env)
            self._add_engine_arg(cmd, "--whisper-server-api-key", "STALIGN_WHISPER_SERVER_API_KEY", env)
        elif engine == "google-cloud":
            self._add_engine_arg(cmd, "--google-cloud-project", "STALIGN_GOOGLE_CLOUD_PROJECT", env)
            self._add_engine_arg(cmd, "--google-cloud-location", "STALIGN_GOOGLE_CLOUD_LOCATION", env)
            self._add_engine_arg(cmd, "--google-cloud-language", "STALIGN_GOOGLE_CLOUD_LANGUAGE", env)
        elif engine == "microsoft-azure":
            self._add_engine_arg(cmd, "--azure-speech-key", "STALIGN_AZURE_SPEECH_KEY", env)
            self._add_engine_arg(cmd, "--azure-speech-region", "STALIGN_AZURE_SPEECH_REGION", env)
            self._add_engine_arg(cmd, "--azure-language", "STALIGN_AZURE_LANGUAGE", env)
        elif engine == "amazon-transcribe":
            self._add_engine_arg(cmd, "--aws-access-key-id", "STALIGN_AWS_ACCESS_KEY_ID", env)
            self._add_engine_arg(cmd, "--aws-secret-access-key", "STALIGN_AWS_SECRET_ACCESS_KEY", env)
            self._add_engine_arg(cmd, "--aws-region", "STALIGN_AWS_REGION", env)
            self._add_engine_arg(cmd, "--aws-language", "STALIGN_AWS_LANGUAGE", env)

        return cmd

    @staticmethod
    def _sanitize_stalign_command(cmd: list[str]) -> list[str]:
        sensitive_flags = {
            "--openai-api-key",
            "--deepgram-api-key",
            "--whisper-server-api-key",
            "--azure-speech-key",
            "--aws-secret-access-key",
            "--aws-access-key-id",
        }
        redacted = []
        skip_next = False
        for i, token in enumerate(cmd):
            if skip_next:
                redacted.append("***")
                skip_next = False
                continue
            redacted.append(token)
            if token in sensitive_flags and i + 1 < len(cmd):
                skip_next = True
        return redacted

    def _find_readaloud_epub(self, output_dir: Path) -> Optional[Path]:
        candidates = sorted(output_dir.rglob("*.epub"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return None
        return candidates[0]

    def transcribe_with_stalign(self, abs_id: str, epub_path: Path, audiobook_dir: Path, engine_config: Optional[dict] = None) -> Optional[Path]:
        stalign_bin = Path(self.stalign_path)
        if not stalign_bin.exists():
            logger.error(f"❌ stalign binary not found at {self.stalign_path}")
            return None

        if not Path(audiobook_dir).exists():
            logger.error(f"❌ stalign audiobook dir does not exist: {audiobook_dir}")
            return None

        output_root = Path(self.data_dir) / "tmp"
        output_root.mkdir(parents=True, exist_ok=True)

        timeout_seconds = max(1, self.stalign_timeout_mins) * 60

        with tempfile.TemporaryDirectory(prefix=f"stalign_{abs_id}_", dir=str(output_root)) as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            output_dir.mkdir(parents=True, exist_ok=True)

            cmd = self._build_stalign_command(Path(epub_path), Path(audiobook_dir), output_dir, engine_config)
            safe_cmd = self._sanitize_stalign_command(cmd)
            logger.info(f"⚙️ Running stalign command: {' '.join(safe_cmd)}")

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                logger.error(f"❌ stalign timed out for '{sanitize_log_data(abs_id)}' after {self.stalign_timeout_mins} minutes")
                return None
            except Exception as run_err:
                logger.error(f"❌ stalign invocation failed: {run_err}")
                return None

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()

            if result.returncode != 0:
                if stdout:
                    logger.error(f"stalign stdout: {stdout}")
                if stderr:
                    logger.error(f"stalign stderr: {stderr}")
                logger.error(f"❌ stalign failed for '{sanitize_log_data(abs_id)}' (exit={result.returncode})")
                return None

            readaloud_path = self._find_readaloud_epub(output_dir)
            if not readaloud_path:
                logger.error("❌ stalign completed but no output EPUB was found")
                return None

            final_output = Path(self.data_dir) / "epub_cache" / f"{abs_id}_readaloud.epub"
            final_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(readaloud_path, final_output)
            logger.info(f"✅ stalign readaloud EPUB generated for '{sanitize_log_data(abs_id)}': {final_output}")
            return final_output

    def _is_low_quality_text(self, text: str, min_word_count: int = 3) -> bool:
        """
        Check if transcript segment text is low-quality for sync purposes.
        
        Low quality includes:
        - Very short segments (< min_word_count words)
        - Audio markers like [Music], [Applause], etc.
        - Empty or whitespace-only text
        - Single-word utterances (often "um", "uh", chapter numbers)
        
        Returns:
            True if the text is considered low quality
        """
        if not text:
            return True
        
        cleaned = text.strip()
        if not cleaned:
            return True
        
        # Check for common audio markers (case-insensitive)
        markers = ['[music]', '[applause]', '[laughter]', '[silence]', '[sound]', 
                   '[inaudible]', '[noise]', '[background]', '♪', '🎵']
        lower_text = cleaned.lower()
        for marker in markers:
            if marker in lower_text:
                return True
        
        # Check word count
        words = cleaned.split()
        if len(words) < min_word_count:
            return True
        
        return False

    def get_text_at_time(self, transcript_path, timestamp):
        """
        Get text context around a specific timestamp.
        Returns ~800 characters of context for better matching.
        
        Uses look-ahead/look-behind when the exact timestamp falls on
        low-quality content (pauses, music, short utterances).
        """
        try:
            data = self._get_cached_transcript(transcript_path)
            if not data:
                return None

            if isinstance(data, StorytellerTranscript):
                return data.get_text_at_time(timestamp)
            if isinstance(data, dict) and self._get_storyteller_timeline(data):
                return self._storyteller_text_at_time(data, timestamp)

            # Find segment containing timestamp
            target_idx = -1
            for i, seg in enumerate(data):
                if seg['start'] <= timestamp <= seg['end']:
                    target_idx = i
                    break

            # Fallback: find closest segment
            if target_idx == -1:
                closest_dist = float('inf')
                for i, seg in enumerate(data):
                    dist = min(abs(timestamp - seg['start']), abs(timestamp - seg['end']))
                    if dist < closest_dist:
                        closest_dist = dist
                        target_idx = i

            if target_idx == -1:
                return None

            # Look-ahead/look-behind: If current segment has low-quality text,
            # search nearby segments for better content
            original_idx = target_idx
            if self._is_low_quality_text(data[target_idx]['text']):
                # Prefer forward (look-ahead) slightly, but also check behind
                # Offsets in segments: try +1, +2, -1, +3, -2, +4, -3, etc.
                offsets = [1, 2, -1, 3, -2, 4, -3, 5]
                for offset in offsets:
                    alt_idx = target_idx + offset
                    if 0 <= alt_idx < len(data):
                        if not self._is_low_quality_text(data[alt_idx]['text']):
                            logger.debug(f"🔍 Look-ahead: Skipped low-quality segment at {data[original_idx]['start']:.1f}s, using segment at {data[alt_idx]['start']:.1f}s instead")
                            target_idx = alt_idx
                            break

            # Gather surrounding context (~800 chars)
            segments_indices = [target_idx]
            current_len = len(data[target_idx]['text'])
            left, right = target_idx - 1, target_idx + 1
            TARGET_LEN = 800

            while current_len < TARGET_LEN:
                added = False
                if left >= 0:
                    segments_indices.insert(0, left)
                    current_len += len(data[left]['text'])
                    left -= 1
                    added = True
                if current_len >= TARGET_LEN:
                    break
                if right < len(data):
                    segments_indices.append(right)
                    current_len += len(data[right]['text'])
                    right += 1
                    added = True
                if not added:
                    break

            raw_text = " ".join([data[i]['text'] for i in segments_indices])
            return self._clean_text(raw_text)

        except Exception as e:
            logger.error(f"❌ Error reading transcript '{transcript_path}': {e}")
        return None

    def get_previous_segment_text(self, transcript_path, timestamp):
        """
        Get the text of the segment immediately preceding the one at timestamp.
        """
        try:
            data = self._get_cached_transcript(transcript_path)
            if not data:
                return None

            if isinstance(data, StorytellerTranscript):
                previous_ts = max(0.0, float(timestamp) - 0.5)
                return data.get_text_at_time(previous_ts)
            if isinstance(data, dict) and self._get_storyteller_timeline(data):
                previous_ts = max(0.0, float(timestamp) - 0.5)
                return self._storyteller_text_at_time(data, previous_ts)

            # Find segment containing timestamp
            target_idx = -1
            for i, seg in enumerate(data):
                if seg['start'] <= timestamp <= seg['end']:
                    target_idx = i
                    break
            
            # If explicit match not found, find closest
            if target_idx == -1:
                closest_dist = float('inf')
                for i, seg in enumerate(data):
                    dist = min(abs(timestamp - seg['start']), abs(timestamp - seg['end']))
                    if dist < closest_dist:
                        closest_dist = dist
                        target_idx = i

            if target_idx > 0:
                prev_text = data[target_idx - 1]['text']
                return self._clean_text(prev_text)
            
            return None

        except Exception as e:
            logger.error(f"❌ Error getting previous segment '{transcript_path}': {e}")
            return None

    @time_execution
    def align_transcript_to_text(self, transcript_segments, full_book_text):
        """
        Creates a mapping of {character_index: timestamp} using Anchored Alignment.
        Uses unique N-grams (N=6) as anchors and linear interpolation for gaps.
        """
        if not transcript_segments or not full_book_text:
            return None

        logger.info(f"🧩 Starting Anchored Alignment (Text: {len(full_book_text)} chars, Segments: {len(transcript_segments)})")

        # 1. Tokenize Transcript into words with timestamps
        transcript_words = []
        for seg in transcript_segments:
            words = seg['text'].split()
            if not words: continue
            
            # Simple duration-based word splitting within segment
            seg_duration = seg['end'] - seg['start']
            word_duration = seg_duration / len(words)
            
            for i, w in enumerate(words):
                transcript_words.append({
                    "word": self._clean_text(w).lower(),
                    "start": seg['start'] + (i * word_duration),
                    "end": seg['start'] + ((i + 1) * word_duration)
                })

        # 2. Tokenize Book Text into words with character offsets
        # Use regex to find words and their offsets
        book_words = []
        for match in re.finditer(r'\b\w+\b', full_book_text):
            word = match.group().lower()
            book_words.append({
                "word": word,
                "start_char": match.start(),
                "end_char": match.end()
            })

        if not transcript_words or not book_words:
            return None

        # 3. Identify Anchors (Unique N-grams, N=12)
        N = 12
        
        def get_n_grams(word_list, is_transcript=False):
            grams = {}
            for i in range(len(word_list) - N + 1):
                gram_parts = []
                for j in range(N):
                    gram_parts.append(word_list[i+j]['word'])
                gram_text = " ".join(gram_parts)
                
                if gram_text not in grams:
                    grams[gram_text] = []
                
                if is_transcript:
                    grams[gram_text].append({
                        "index": i,
                        "time": word_list[i]['start']
                    })
                else:
                    grams[gram_text].append({
                        "index": i,
                        "char_offset": word_list[i]['start_char']
                    })
            return grams

        t_grams = get_n_grams(transcript_words, True)
        b_grams = get_n_grams(book_words, False)

        # Find anchors (unique in both)
        anchors = []
        for gram_text, t_matches in t_grams.items():
            if len(t_matches) == 1 and gram_text in b_grams and len(b_grams[gram_text]) == 1:
                anchors.append({
                    "time": t_matches[0]['time'],
                    "char_offset": b_grams[gram_text][0]['char_offset']
                })

        # Sort anchors by offset
        anchors.sort(key=lambda x: x['char_offset'])
        
        # Deduplicate/Filter non-monotonic anchors (rare but possible with hallucinations)
        valid_anchors = []
        if anchors:
            valid_anchors.append(anchors[0])
            for i in range(1, len(anchors)):
                if anchors[i]['time'] > valid_anchors[-1]['time']:
                    valid_anchors.append(anchors[i])
        
        logger.info(f"⚓ Found {len(valid_anchors)} unique anchors for alignment.")

        if not valid_anchors:
            return None

        # 4. Fill gaps with linear interpolation
        # Result is a list of points (char_offset, timestamp)
        alignment_points = []
        
        # Start of book to first anchor
        if valid_anchors[0]['char_offset'] > 0:
            alignment_points.append({"char": 0, "ts": 0.0})
        
        # Between anchors
        for i in range(len(valid_anchors)):
            alignment_points.append({"char": valid_anchors[i]['char_offset'], "ts": valid_anchors[i]['time']})
            
            if i < len(valid_anchors) - 1:
                # Add a few points between anchors to smooth things out
                # or just let the caller interpolate. Let's provide a dense enough map.
                pass

        # Last anchor to end of book
        total_audio_duration = transcript_segments[-1]['end']
        if valid_anchors[-1]['char_offset'] < len(full_book_text):
            alignment_points.append({"char": len(full_book_text), "ts": total_audio_duration})

        return alignment_points

    @time_execution
    def find_time_for_text(self, transcript_path, search_text, hint_percentage=None, char_offset=None, book_title=None) -> Optional[float]:
        """
        Find timestamp for given text using windowed fuzzy matching or pre-computed alignment map.
        """
        from rapidfuzz import fuzz
        title_prefix = f"[{sanitize_log_data(book_title)}] " if book_title else ""

        try:
            # NOTE: Alignment map lookups are now handled by AlignmentService (database-backed).
            # The synchronization layer (ABSSyncClient) should use alignment_service.find_time_for_position()
            # for precise char_offset to timestamp conversion.
            # This method now only handles fallback fuzzy text matching for legacy paths.
            
            # Fuzzy text matching (fallback when alignment map not available)
            data = self._get_cached_transcript(transcript_path)
            if not data:
                return None

            if isinstance(data, StorytellerTranscript):
                chapter_index = None
                local_offset = None

                if isinstance(char_offset, dict):
                    chapter_index = char_offset.get("chapter")
                    local_offset = char_offset.get("offset")
                elif isinstance(char_offset, (list, tuple)) and len(char_offset) == 2:
                    chapter_index, local_offset = char_offset[0], char_offset[1]

                if chapter_index is None or local_offset is None:
                    synthetic_data = []
                    for idx, meta in enumerate(data.chapters):
                        try:
                            chapter = data._load_chapter(idx)
                            chapter_start = float(meta.get("start", 0.0) or 0.0)
                            transcript_text = chapter.get("transcript", "")
                            timeline = chapter.get("word_timeline", [])
                            if not timeline:
                                continue

                            seg_start = chapter_start + float(timeline[0].get("startTime", 0.0) or 0.0)
                            seg_text_words = []

                            for i, word in enumerate(timeline):
                                ts = chapter_start + float(word.get("startTime", 0.0) or 0.0)
                                word_text = word.get("word")
                                if not word_text:
                                    py_start = chapter["start_offsets_py"][i] if i < len(chapter["start_offsets_py"]) else 0
                                    py_end = chapter["start_offsets_py"][i + 1] if i + 1 < len(chapter["start_offsets_py"]) else len(transcript_text)
                                    word_text = transcript_text[py_start:py_end]

                                cleaned_word = self._clean_text(word_text)
                                if cleaned_word:
                                    seg_text_words.append(cleaned_word)

                                if ts - seg_start > 5.0 or i == len(timeline) - 1:
                                    segment_text = " ".join(seg_text_words).strip()
                                    if segment_text:
                                        synthetic_data.append({
                                            "start": seg_start,
                                            "end": ts + 0.5,
                                            "text": segment_text
                                        })
                                    seg_start = ts
                                    seg_text_words = []
                        except Exception:
                            continue

                    data = synthetic_data
                    if not synthetic_data:
                        return None
                else:
                    local_ts = data.char_offset_to_timestamp(int(local_offset), int(chapter_index))
                    if local_ts is None:
                        return None

                    chapter_meta = data.chapters[int(chapter_index)] if int(chapter_index) < len(data.chapters) else {}
                    chapter_start = float(chapter_meta.get("start", 0.0) or 0.0)
                    return chapter_start + float(local_ts)

            if isinstance(data, dict) and self._get_storyteller_timeline(data):
                if char_offset is not None:
                    return self._storyteller_time_for_offset(data, int(char_offset))

                transcript_text = self._clean_text(data.get("transcript", ""))
                clean_search = self._clean_text(search_text)
                if not clean_search or not transcript_text:
                    return None
                search_idx = transcript_text.lower().find(clean_search.lower())
                if search_idx < 0:
                    return None
                return self._storyteller_time_for_offset(data, search_idx)

            clean_search = self._clean_text(search_text)

            # Build windows for searching
            windows = []
            window_size = 12

            for i in range(0, len(data), window_size // 2):
                window_segments = data[i:min(i + window_size, len(data))]
                window_text = " ".join([seg['text'] for seg in window_segments])
                windows.append({
                    'start': data[i]['start'],
                    'end': window_segments[-1]['end'],
                    'text': self._clean_text(window_text),
                    'index': i
                })

            if not windows:
                return None

            best_match = None
            best_score = 0

            # First: search near hint if provided
            if hint_percentage is not None:
                total_duration = data[-1]['end']
                hint_start = max(0, hint_percentage - 0.15) * total_duration
                hint_end = min(1.0, hint_percentage + 0.15) * total_duration
                nearby_windows = [w for w in windows if w['start'] >= hint_start and w['start'] <= hint_end]

                for window in nearby_windows:
                    score = fuzz.token_set_ratio(clean_search, window['text'])
                    if score > best_score:
                        best_score = score
                        best_match = window

                if best_score >= self.match_threshold:
                    logger.info(f"✅ {title_prefix}Match found at {best_match['start']:.1f}s | Confidence: {best_score}% - '{sanitize_log_data(clean_search)}'")
                    return best_match['start']

            # Second: search all windows
            for window in windows:
                score = fuzz.token_set_ratio(clean_search, window['text'])
                if score > best_score:
                    best_score = score
                    best_match = window

            if best_match and best_score >= self.match_threshold:
                logger.info(f"✅ {title_prefix}Match found at {best_match['start']:.1f}s | Confidence: {best_score}% - '{sanitize_log_data(clean_search)}'")
                return best_match['start']
            else:
                logger.warning(f"⚠️ {title_prefix}No good match found (best: {best_score}% < {self.match_threshold}%)")
                return None

        except Exception as e:
            logger.error(f"❌ {title_prefix}Error searching transcript '{transcript_path}': {e}")
        return None
# [END FILE]

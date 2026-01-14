# [START FILE: abs-kosync-enhanced/transcriber.py]
"""
Audio Transcriber for abs-kosync-enhanced

UPDATED VERSION with:
- WAV normalization fix for ctranslate2/faster-whisper codec compatibility
- LRU transcript cache
- Long file splitting
- Configurable fuzzy match threshold
- Context gathering for text matching
- Dependency Injection for SmilExtractor
"""

import json
import logging
import os
import shutil
import subprocess
import gc
from pathlib import Path
from typing import Optional

from faster_whisper import WhisperModel
import requests
import math
from collections import OrderedDict
import re

from src.utils.logging_utils import sanitize_log_data, time_execution
# We keep the import for type hinting, but we don't instantiate it directly anymore
from src.utils.smil_extractor import SmilExtractor 

logger = logging.getLogger(__name__)


class AudioTranscriber:
    # [UPDATED] Accepted smil_extractor as an argument
    def __init__(self, data_dir, smil_extractor):
        self.data_dir = data_dir
        self.transcripts_dir = data_dir / "transcripts"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.cache_root = data_dir / "audio_cache"
        self.cache_root.mkdir(parents=True, exist_ok=True)

        self.model_size = os.environ.get("WHISPER_MODEL", "base")

        self._transcript_cache = OrderedDict()
        self._cache_capacity = 3

        # Unified threshold logic
        self.match_threshold = int(os.environ.get("TRANSCRIPT_MATCH_THRESHOLD", os.environ.get("FUZZY_MATCH_THRESHOLD", 80)))
        
        # [UPDATED] Use the injected instance
        self.smil_extractor = smil_extractor

    def transcribe_from_smil(self, abs_id: str, epub_path: Path, abs_chapters: list) -> Optional[Path]:
        """
        Attempts to extract a transcript directly from the EPUB's SMIL overlay data.
        """
        output_file = self.transcripts_dir / f"{abs_id}.json"
        
        if not self.smil_extractor.has_media_overlays(str(epub_path)):
            return None
            
        logger.info(f"⚡ Fast-Path: Extracting transcript from SMIL for {abs_id}...")
        
        try:
            transcript = self.smil_extractor.extract_transcript(str(epub_path), abs_chapters)
            if not transcript:
                return None
                
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(transcript, f, ensure_ascii=False)
                
            logger.info(f"✅ SMIL Extraction complete: {len(transcript)} segments saved.")
            return output_file
        except Exception as e:
            logger.error(f"Failed to extract SMIL transcript: {e}")
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
            self._transcript_cache[path_str] = data
            self._transcript_cache.move_to_end(path_str)
            if len(self._transcript_cache) > self._cache_capacity:
                self._transcript_cache.popitem(last=False)
            return data
        except Exception as e:
            logger.error(f"Error loading transcript {path}: {e}")
            return None

    def _clean_text(self, text):
        """Aggressive text cleaner to boost fuzzy match scores."""
        if not text:
            return ""
        return re.sub(r'\s+', ' ', text).strip()

    def get_audio_duration(self, file_path):
        """Get duration of audio file using ffprobe."""
        cmd = [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)
        ]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return float(result.stdout.strip())
        except (ValueError, subprocess.CalledProcessError) as e:
            logger.error(f"Could not determine duration for {file_path}: {e}")
            return 0.0

    def normalize_audio_to_wav(self, input_path: Path) -> Path:
        """
        Convert any audio file to a standardized WAV format that faster-whisper can reliably decode.

        This fixes codec compatibility issues with ctranslate2/faster-whisper by ensuring
        we always feed it a known-good format: 16kHz mono 16-bit PCM WAV.

        Args:
            input_path: Path to the input audio file (any format FFmpeg supports)

        Returns:
            Path to the normalized WAV file, or None on failure
        """
        output_path = input_path.with_suffix('.wav')

        # If input is already a WAV, still convert to ensure proper format
        if input_path.suffix.lower() == '.wav':
            output_path = input_path.with_name(f"{input_path.stem}_normalized.wav")

        logger.info(f"   🔄 Normalizing: {input_path.name} → WAV")

        cmd = [
            'ffmpeg', '-y',
            '-i', str(input_path),
            '-ar', '16000',      # 16kHz sample rate (optimal for Whisper)
            '-ac', '1',          # Mono
            '-c:a', 'pcm_s16le', # 16-bit PCM (most compatible)
            '-f', 'wav',         # Force WAV container
            '-loglevel', 'error',
            str(output_path)
        ]

        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)

            # Remove original if different from output to save space
            if input_path != output_path and input_path.exists():
                input_path.unlink()

            logger.debug(f"   ✓ Normalized: {output_path.name}")
            return output_path

        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg conversion failed for {input_path}: {e.stderr}")
            return None

    def split_audio_file(self, file_path, target_max_duration_sec=2700):
        """Split long audio files into smaller chunks, outputting as WAV."""
        duration = self.get_audio_duration(file_path)
        if duration <= target_max_duration_sec:
            return [file_path]

        logger.info(f"   ⚠️ File {file_path.name} is {duration/60:.1f}m. Splitting...")
        num_parts = math.ceil(duration / target_max_duration_sec)
        segment_duration = duration / num_parts
        new_files = []
        base_name = file_path.stem.replace('_normalized', '')  # Clean up name

        for i in range(num_parts):
            start_time = i * segment_duration
            # Output as WAV for consistency
            new_filename = f"{base_name}_split_{i+1:03d}.wav"
            new_path = file_path.parent / new_filename
            cmd = [
                'ffmpeg', '-y',
                '-i', str(file_path),
                '-ss', str(start_time),
                '-t', str(segment_duration),
                '-ar', '16000',      # 16kHz
                '-ac', '1',          # Mono
                '-c:a', 'pcm_s16le', # PCM WAV
                '-f', 'wav',
                '-loglevel', 'error',
                str(new_path)
            ]
            try:
                subprocess.run(cmd, check=True)
                new_files.append(new_path)
                logger.info(f"      Created chunk {i+1}/{num_parts}: {new_filename}")
            except subprocess.CalledProcessError as e:
                logger.error(f"      Failed to create chunk {i+1}: {e}")

        # Remove original file after splitting
        if new_files:
            try:
                file_path.unlink()
            except:
                pass

        return new_files if new_files else [file_path]

    @time_execution
    def process_audio(self, abs_id, audio_urls):
        output_file = self.transcripts_dir / f"{abs_id}.json"
        if output_file.exists():
            logger.info(f"Transcript already exists for {abs_id}")
            return output_file

        book_cache_dir = self.cache_root / str(abs_id)
        if book_cache_dir.exists():
            # If we aren't resuming, clean up
            pass
        book_cache_dir.mkdir(parents=True, exist_ok=True)

        progress_file = book_cache_dir / "_progress.json"
        MAX_DURATION_SECONDS = 45 * 60

        downloaded_files = []
        full_transcript = []
        chunks_completed = 0
        cumulative_duration = 0.0
        resuming = False

        try:
            # Check for resumption
            if progress_file.exists():
                try:
                    with open(progress_file, 'r') as f:
                        progress = json.load(f)
                    chunks_completed = progress.get('chunks_completed', 0)
                    cumulative_duration = progress.get('cumulative_duration', 0.0)
                    full_transcript = progress.get('transcript', [])

                    # Find existing split files
                    cached_files = sorted(book_cache_dir.glob("part_*_split_*.wav"))

                    if cached_files and chunks_completed > 0:
                        downloaded_files = list(cached_files)
                        resuming = True
                        logger.info(f"♻️ Resuming transcription: {chunks_completed} chunks previously done")
                except Exception as e:
                    logger.warning(f"⚠️ Could not resume (will start fresh): {e}")
                    if book_cache_dir.exists(): shutil.rmtree(book_cache_dir)
                    book_cache_dir.mkdir(parents=True, exist_ok=True)
                    resuming = False

            # Phase 1: Download and Normalize (if not resuming)
            if not resuming:
                # FIX: Check if files exist from a previous run before wiping
                existing_files = sorted(book_cache_dir.glob("part_*_split_*.wav"))

                if existing_files:
                    logger.info(f"♻️ Found {len(existing_files)} existing split files. Skipping download phase.")
                    downloaded_files = list(existing_files)
                else:
                    # Original logic: Wipe and Start Fresh
                    if book_cache_dir.exists(): shutil.rmtree(book_cache_dir)
                    book_cache_dir.mkdir(parents=True, exist_ok=True)
                    downloaded_files = []

                    logger.info(f"📥 Phase 1: Downloading {len(audio_urls)} audio files...")
                    for idx, audio_data in enumerate(audio_urls):
                        stream_url = audio_data['stream_url']
                        extension = audio_data.get('ext', '.mp3')
                        if not extension.startswith('.'): extension = f".{extension}"
                        local_path = book_cache_dir / f"part_{idx:03d}{extension}"

                        logger.info(f"   Downloading Part {idx + 1}/{len(audio_urls)}...")
                        with requests.get(stream_url, stream=True, timeout=300) as r:
                            r.raise_for_status()
                            with open(local_path, 'wb') as f:
                                for chunk in r.iter_content(chunk_size=8192):
                                    f.write(chunk)

                        if not local_path.exists() or local_path.stat().st_size == 0:
                            raise ValueError(f"File {local_path} is empty or missing.")

                        # Normalize to WAV
                        normalized_path = self.normalize_audio_to_wav(local_path)
                        if not normalized_path:
                            raise ValueError(f"Normalization failed for part {idx+1}")

                        # Split if needed
                        downloaded_files.extend(self.split_audio_file(normalized_path, MAX_DURATION_SECONDS))

                    if not downloaded_files:
                        raise ValueError("No audio files were successfully downloaded and normalized")

                if not downloaded_files:
                    raise ValueError("No audio files were successfully downloaded and normalized")

            # Phase 2: Transcribe
            logger.info(f"✅ All parts cached. Starting transcription ({len(downloaded_files)} chunks)...")
            logger.info(f"🧠 Phase 2: Transcribing using {self.model_size} model...")

            model = WhisperModel(self.model_size, device="cpu", compute_type="int8", cpu_threads=4)

            total_chunks = len(downloaded_files)
            # Calculate total audio duration for progress reporting
            total_audio_duration = sum(self.get_audio_duration(f) for f in downloaded_files)

            for idx, local_path in enumerate(downloaded_files):
                # Skip already-completed chunks when resuming
                if idx < chunks_completed:
                    continue

                duration = self.get_audio_duration(local_path)
                pct = (cumulative_duration / total_audio_duration * 100) if total_audio_duration > 0 else 0
                logger.info(f"   [{pct:.0f}%] Transcribing chunk {idx + 1}/{total_chunks} ({duration/60:.1f} min)...")

                try:
                    segments, info = model.transcribe(str(local_path), beam_size=1, best_of=1)

                    segment_count = 0
                    for segment in segments:
                        full_transcript.append({
                            "start": segment.start + cumulative_duration,
                            "end": segment.end + cumulative_duration,
                            "text": segment.text.strip()
                        })
                        segment_count += 1

                except Exception as e:
                    logger.error(f"   ❌ Transcription failed for {local_path.name}: {e}")
                    raise

                cumulative_duration += duration
                chunks_completed = idx + 1

                # Save progress after each chunk for resumption
                with open(progress_file, 'w') as f:
                    json.dump({
                        'chunks_completed': chunks_completed,
                        'cumulative_duration': cumulative_duration,
                        'transcript': full_transcript
                    }, f)

                gc.collect()

            # Save final transcript
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(full_transcript, f, ensure_ascii=False)

            logger.info(f"✅ Transcription complete: {len(full_transcript)} segments, {cumulative_duration/60:.1f} minutes")

            # Clean up cache only on success
            if book_cache_dir.exists():
                shutil.rmtree(book_cache_dir)

            return output_file

        except Exception as e:
            logger.error(f"❌ Transcription failed: {e}")
            if output_file.exists():
                os.remove(output_file)
            # Don't delete cache dir - allows resume on retry
            raise e

    def get_text_at_time(self, transcript_path, timestamp):
        """
        Get text context around a specific timestamp.
        Returns ~800 characters of context for better matching.
        """
        try:
            data = self._get_cached_transcript(transcript_path)
            if not data:
                return None

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
            logger.error(f"Error reading transcript {transcript_path}: {e}")
        return None

    @time_execution
    def find_time_for_text(self, transcript_path, search_text, hint_percentage=None, book_title=None) -> Optional[float]:
        """
        Find timestamp for given text using windowed fuzzy matching.

        Args:
            transcript_path: Path to transcript JSON
            search_text: Text to search for
            hint_percentage: Optional hint for where to search first (0.0-1.0)
            book_title: Optional book title for logging purposes

        Returns:
            Timestamp in seconds, or None if not found
        """
        from rapidfuzz import fuzz
        title_prefix = f"[{sanitize_log_data(book_title)}] " if book_title else ""

        try:
            data = self._get_cached_transcript(transcript_path)
            if not data:
                return None

            clean_search = self._clean_text(search_text)

            # Build windows for searching
            windows = []
            window_size = 12

            for i in range(0, len(data), window_size // 2):
                window_text = " ".join([data[j]['text'] for j in range(i, min(i + window_size, len(data)))])
                windows.append({
                    'start': data[i]['start'],
                    'end': data[min(i + window_size, len(data))-1]['end'],
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
                title_prefix = f"[{sanitize_log_data(book_title)}] " if book_title else ""
                logger.info(f"✅ {title_prefix}Match found at {best_match['start']:.1f}s | Confidence: {best_score}% - '{sanitize_log_data(clean_search)}'")
                return best_match['start']
            else:

                logger.warning(f"{title_prefix}No good match found (best: {best_score}% < {self.match_threshold}%)")
                return None

        except Exception as e:
            logger.error(f"{title_prefix}Error searching transcript {transcript_path}: {e}")
        return None
# [END FILE]
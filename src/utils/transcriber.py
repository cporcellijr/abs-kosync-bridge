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
from src.utils.transcription_providers import get_transcription_provider
# We keep the import for type hinting, but we don't instantiate it directly anymore
from rapidfuzz import fuzz, process

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
        
        # GPU/Device configuration
        self.whisper_device = os.environ.get("WHISPER_DEVICE", "auto").lower()
        self.whisper_compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "auto").lower()

        self._transcript_cache = OrderedDict()
        self._cache_capacity = 3

        # Unified threshold logic
        self.match_threshold = int(os.environ.get("TRANSCRIPT_MATCH_THRESHOLD", os.environ.get("FUZZY_MATCH_THRESHOLD", 80)))

        # [UPDATED] Use the injected instance
        self.smil_extractor = smil_extractor

    def _get_whisper_config(self) -> tuple[str, str]:
        """
        Determine the Whisper device and compute type based on configuration.
        
        Returns:
            (device, compute_type) tuple
        
        Configuration options:
            WHISPER_DEVICE: 'auto', 'cpu', 'cuda'
            WHISPER_COMPUTE_TYPE: 'auto', 'int8', 'float16', 'float32'
        
        When 'auto', attempts CUDA detection with graceful fallback to CPU.
        """
        device = self.whisper_device
        compute_type = self.whisper_compute_type
        
        if device == 'auto':
            try:
                import torch
                if torch.cuda.is_available():
                    device = 'cuda'
                    logger.info(f"🎮 CUDA available: {torch.cuda.get_device_name(0)}")
                else:
                    device = 'cpu'
                    logger.info("💻 CUDA not available, using CPU")
            except ImportError:
                device = 'cpu'
                logger.info("💻 PyTorch not installed, using CPU")
        
        if compute_type == 'auto':
            # float16 for GPU, int8 for CPU (optimal defaults)
            compute_type = 'float16' if device == 'cuda' else 'int8'
        
        logger.info(f"⚙️ Whisper config: device={device}, compute_type={compute_type}, model={self.model_size}")
        return device, compute_type

    def validate_transcript(self, segments: list, max_overlap_ratio: float = 0.05) -> tuple[bool, float]:
        """
        Validate transcript for overlapping timestamps.
        
        Returns:
            (is_valid, overlap_ratio)
        """
        if not segments or len(segments) < 2:
            return True, 0.0
        
        overlap_count = 0
        for i in range(1, len(segments)):
            if segments[i]['start'] < segments[i-1]['end']:
                overlap_count += 1
        
        overlap_ratio = overlap_count / len(segments)
        is_valid = overlap_ratio <= max_overlap_ratio
        
        return is_valid, overlap_ratio

    def transcribe_from_smil(self, abs_id: str, epub_path: Path, abs_chapters: list, progress_callback=None) -> Optional[Path]:
        """
        Attempts to extract a transcript directly from the EPUB's SMIL overlay data.
        """
        if progress_callback: progress_callback(0.0)
        output_file = self.transcripts_dir / f"{abs_id}.json"

        if not self.smil_extractor.has_media_overlays(str(epub_path)):
            return None

        logger.info(f"⚡ Fast-Path: Extracting transcript from SMIL for {abs_id}...")

        try:
            transcript = self.smil_extractor.extract_transcript(str(epub_path), abs_chapters)
            if not transcript:
                return None

            # [NEW] Validate transcript before saving
            is_valid, overlap_ratio = self.validate_transcript(transcript)
            
            if not is_valid:
                logger.warning(f"⚠️ SMIL extraction failed validation: {overlap_ratio:.1%} overlap (threshold: 5%)")
                logger.info(f"🔄 Falling back to Whisper transcription for {abs_id}")
                
                return None

            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(transcript, f, ensure_ascii=False)

            # [NEW] Generate alignment map for SMIL path
            try:
                # Need to read epub text
                from src.utils.ebook_utils import EbookParser
                # We might not have ebook_parser here, but we can extract it if needed
                # Actually, SyncManager already has it. But for now, let's keep it simple.
                # If we want alignment on SMIL, we need the full book text.
                pass
            except:
                pass

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

    def normalize_audio_to_wav(self, input_path: Path) -> Optional[Path]:
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
            subprocess.run(cmd, check=True, capture_output=True, text=True)

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
    def process_audio(self, abs_id, audio_urls, full_book_text=None, progress_callback=None):
        output_file = self.transcripts_dir / f"{abs_id}.json"
        
        # [NEW] Path for the alignment map
        alignment_file = self.transcripts_dir / f"{abs_id}_alignment.json"

        # Check if we can skip the heavy lifting
        if output_file.exists():
            # If we have the transcript AND the map (or no text to map against), we are truly done.
            if not full_book_text or alignment_file.exists():
                logger.info(f"Transcript and alignment already exist for {abs_id}")
                return output_file
            
            # If we are here, we have the transcript but MISS the map.
            # We can skip downloading/transcribing and jump straight to alignment!
            logger.info(f"⚡ Transcript exists for {abs_id}, but alignment map is missing. Running alignment phase only.")
            try:
                with open(output_file, 'r', encoding='utf-8') as f:
                    full_transcript = json.load(f)
                
                # Run Phase 3 (Alignment) immediately
                alignment_map = self.align_transcript_to_text(full_transcript, full_book_text)
                if alignment_map:
                    with open(alignment_file, 'w', encoding='utf-8') as f:
                        json.dump(alignment_map, f, ensure_ascii=False)
                    logger.info(f"✅ Alignment complete: Saved {len(alignment_map)} sync points.")
                
                return output_file
            except Exception as e:
                logger.error(f"⚠️ Alignment update failed: {e}")
                # If alignment fails, we still return the output_file so basic sync works
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
                # FIX: Check if files exist for ALL parts before skipping
                existing_files = sorted(book_cache_dir.glob("part_*_split_*.wav"))
                
                # Check coverage: Do we have at least one file for every index in audio_urls?
                missing_parts = False
                for idx in range(len(audio_urls)):
                    # Look for any file starting with part_{idx:03d}
                    part_exists = any(f.name.startswith(f"part_{idx:03d}_") for f in existing_files)
                    if not part_exists:
                        missing_parts = True
                        break
                
                if existing_files and not missing_parts:
                    logger.info(f"♻️ Found valid cache ({len(existing_files)} files covering all {len(audio_urls)} parts). Skipping download.")
                    downloaded_files = list(existing_files)
                else:
                    if existing_files:
                        logger.warning(f"⚠️ Found {len(existing_files)} cached files but some parts are missing. Wiping cache to start fresh.")
                        shutil.rmtree(book_cache_dir)
                    
                    # Original logic: Wipe and Start Fresh
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
            provider = get_transcription_provider()
            logger.info(f"🧠 Phase 2: Transcribing using {provider.get_name()}...")

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
                    # Use the transcription provider
                    segments = provider.transcribe(local_path)
                    
                    for segment in segments:
                        full_transcript.append({
                            "start": segment["start"] + cumulative_duration,
                            "end": segment["end"] + cumulative_duration,
                            "text": segment["text"]
                        })

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

                if progress_callback:
                    # Report progress for this phase (handled by SyncManager logic)
                    progress_callback(chunks_completed / total_chunks)

                gc.collect()

            # Save final transcript
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(full_transcript, f, ensure_ascii=False)

            # [NEW] Phase 3: Alignment (If book text provided)
            if full_book_text:
                try:
                    alignment_map = self.align_transcript_to_text(full_transcript, full_book_text)
                    if alignment_map:
                        with open(alignment_file, 'w', encoding='utf-8') as f:
                            json.dump(alignment_map, f, ensure_ascii=False)
                        logger.info(f"✅ Alignment complete: Saved {len(alignment_map)} sync points.")
                except Exception as e:
                    logger.error(f"⚠️ Alignment failed: {e}")

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
            logger.error(f"Error reading transcript {transcript_path}: {e}")
        return None

    def get_previous_segment_text(self, transcript_path, timestamp):
        """
        Get the text of the segment immediately preceding the one at timestamp.
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
            logger.error(f"Error getting previous segment {transcript_path}: {e}")
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
            # 0. Try Alignment Map First (Precise Path)
            abs_id = Path(transcript_path).stem
            map_file = self.transcripts_dir / f"{abs_id}_alignment.json"
            
            if map_file.exists() and char_offset is not None:
                try:
                    with open(map_file, 'r') as f:
                        points = json.load(f)
                    
                    if points:
                        # Find the two points surrounding char_offset
                        # points: [{"char": 0, "ts": 0.0}, ...]
                        left = 0
                        right = len(points) - 1
                        
                        # Binary search for the neighborhood
                        while left <= right:
                            mid = (left + right) // 2
                            if points[mid]['char'] <= char_offset:
                                left = mid + 1
                            else:
                                right = mid - 1
                        
                        # Neighborhood is at index 'right' and 'right + 1'
                        if right >= 0:
                            p1 = points[right]
                            if right + 1 < len(points):
                                p2 = points[right + 1]
                                # Linear interpolation
                                char_delta = p2['char'] - p1['char']
                                time_delta = p2['ts'] - p1['ts']
                                
                                ratio = (char_offset - p1['char']) / char_delta if char_delta > 0 else 0
                                precise_ts = p1['ts'] + (ratio * time_delta)
                                
                                logger.debug(f"{title_prefix}🎯 PRECISE MATCH (Map) at {precise_ts:.2f}s (Char: {char_offset})")
                                return precise_ts
                            else:
                                logger.debug(f"{title_prefix}🎯 PRECISE MATCH (Map End) at {p1['ts']:.2f}s")
                                return p1['ts']
                except Exception as e:
                    logger.warning(f"{title_prefix}Alignment map lookup failed: {e}")

            # Fallback to Windowed Fuzzy Match (Existing Logic)
            data = self._get_cached_transcript(transcript_path)
            if not data:
                return None

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
                logger.warning(f"{title_prefix}No good match found (best: {best_score}% < {self.match_threshold}%)")
                return None

        except Exception as e:
            logger.error(f"{title_prefix}Error searching transcript {transcript_path}: {e}")
        return None
# [END FILE]

# [START FILE: abs-kosync-enhanced/transcriber.py]
import json
import logging
import os
import shutil
import subprocess
import gc
from pathlib import Path
from faster_whisper import WhisperModel
import requests
import math
from collections import OrderedDict
import re

logger = logging.getLogger(__name__)

class AudioTranscriber:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.transcripts_dir = data_dir / "transcripts"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.cache_root = data_dir / "audio_cache"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        
        self.model_size = os.environ.get("WHISPER_MODEL", "tiny")
        
        self._transcript_cache = OrderedDict()
        self._cache_capacity = 3
        
        # CHANGED: Unified threshold logic.
        # Prefer specific TRANSCRIPT_MATCH_THRESHOLD, fallback to general FUZZY_MATCH_THRESHOLD, default to 80.
        self.match_threshold = int(os.environ.get("TRANSCRIPT_MATCH_THRESHOLD", os.environ.get("FUZZY_MATCH_THRESHOLD", 80)))

    def _get_cached_transcript(self, path):
        path_str = str(path)
        if path_str in self._transcript_cache:
            self._transcript_cache.move_to_end(path_str)
            return self._transcript_cache[path_str]
        
        try:
            with open(path, 'r') as f:
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
        if not text: return ""
        # Replace newlines and multiple spaces with single space
        return re.sub(r'\s+', ' ', text).strip()

    def get_audio_duration(self, file_path):
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

    def split_audio_file(self, file_path, target_max_duration_sec=2700):
        duration = self.get_audio_duration(file_path)
        if duration <= target_max_duration_sec: return [file_path]

        logger.info(f"⚠️ File {file_path.name} is {duration/60:.2f}m. Splitting...")
        num_parts = math.ceil(duration / target_max_duration_sec)
        segment_duration = duration / num_parts
        new_files = []
        base_name = file_path.stem
        extension = file_path.suffix

        for i in range(num_parts):
            start_time = i * segment_duration
            new_filename = f"{base_name}_split_{i+1:03d}{extension}"
            new_path = file_path.parent / new_filename
            cmd = [
                'ffmpeg', '-y', '-i', str(file_path), '-ss', str(start_time),
                '-t', str(segment_duration), '-map', '0:a', '-c', 'copy',  
                '-loglevel', 'error', str(new_path)
            ]
            subprocess.run(cmd, check=True)
            new_files.append(new_path)
            logger.info(f"  Created chunk {i+1}/{num_parts}: {new_filename}")

        file_path.unlink() 
        return new_files

    def process_audio(self, abs_id, audio_urls):
        output_file = self.transcripts_dir / f"{abs_id}.json"
        if output_file.exists():
            logger.info(f"Transcript already exists for {abs_id}")
            return output_file

        book_cache_dir = self.cache_root / str(abs_id)
        progress_file = book_cache_dir / "_progress.json"
        MAX_DURATION_SECONDS = 45 * 60

        # Try to resume from previous state, fall back to fresh start on any error
        downloaded_files = []
        full_transcript = []
        chunks_completed = 0
        cumulative_duration = 0.0
        resuming = False

        try:
            if progress_file.exists():
                try:
                    with open(progress_file, 'r') as f:
                        progress = json.load(f)
                    chunks_completed = progress.get('chunks_completed', 0)
                    cumulative_duration = progress.get('cumulative_duration', 0.0)
                    full_transcript = progress.get('transcript', [])
                    cached_files = sorted(book_cache_dir.glob("part_*_split_*.mp3")) or \
                                   sorted(book_cache_dir.glob("part_*_split_*.m4b")) or \
                                   sorted(book_cache_dir.glob("part_*.mp3")) or \
                                   sorted(book_cache_dir.glob("part_*.m4b"))
                    if cached_files and chunks_completed > 0:
                        downloaded_files = list(cached_files)
                        resuming = True
                        logger.info(f"♻️ Resuming transcription: {chunks_completed}/{len(downloaded_files)} chunks done")
                except Exception as e:
                    logger.warning(f"⚠️ Could not resume (will start fresh): {e}")
                    if book_cache_dir.exists(): shutil.rmtree(book_cache_dir)
                    downloaded_files = []
                    full_transcript = []
                    chunks_completed = 0
                    cumulative_duration = 0.0

            # Fresh start: download all files
            if not resuming:
                if book_cache_dir.exists(): shutil.rmtree(book_cache_dir)
                book_cache_dir.mkdir(parents=True, exist_ok=True)

                total_files = len(audio_urls)
                logger.info(f"📥 Phase 1/2: Downloading {total_files} audio file(s)...")
                for idx, audio_data in enumerate(audio_urls):
                    stream_url = audio_data['stream_url']
                    extension = audio_data.get('ext', '.mp3')
                    local_path = book_cache_dir / f"part_{idx:03d}{extension}"

                    pct = ((idx + 1) / total_files) * 100
                    logger.info(f"   [{pct:.0f}%] Downloading file {idx + 1}/{total_files}...")
                    with requests.get(stream_url, stream=True, timeout=120) as r:
                        r.raise_for_status()
                        with open(local_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192): f.write(chunk)

                    if not local_path.exists() or local_path.stat().st_size == 0:
                        raise ValueError(f"File {local_path} is empty or missing.")

                    downloaded_files.extend(self.split_audio_file(local_path, MAX_DURATION_SECONDS))

                logger.info(f"✅ Download complete. {len(downloaded_files)} chunk(s) to transcribe.")

            logger.info(f"🧠 Phase 2/2: Transcribing with Whisper ({self.model_size} model)...")

            # Calculate total audio duration for progress reporting
            total_audio_duration = sum(self.get_audio_duration(f) for f in downloaded_files)
            total_mins = total_audio_duration / 60
            logger.info(f"   Total audio duration: {total_mins:.1f} minutes")

            model = WhisperModel(self.model_size, device="cpu", compute_type="int8", cpu_threads=4)
            total_chunks = len(downloaded_files)

            for idx, local_path in enumerate(downloaded_files):
                # Skip already-completed chunks when resuming
                if idx < chunks_completed:
                    continue

                duration = self.get_audio_duration(local_path)

                # Log progress before starting this chunk
                pct = (cumulative_duration / total_audio_duration * 100) if total_audio_duration > 0 else 0
                logger.info(f"   [{pct:.0f}%] Transcribing chunk {idx + 1}/{total_chunks} ({duration/60:.1f} min)...")

                segments, info = model.transcribe(str(local_path), beam_size=1, best_of=1)

                for segment in segments:
                    full_transcript.append({
                        "start": segment.start + cumulative_duration,
                        "end": segment.end + cumulative_duration,
                        "text": segment.text.strip()
                    })
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

            with open(output_file, 'w') as f: json.dump(full_transcript, f)

            logger.info(f"✅ Transcription complete: {len(full_transcript)} segments")

            # Clean up cache only on success
            if book_cache_dir.exists(): shutil.rmtree(book_cache_dir)

            return output_file

        except Exception as e:
            logger.error(f"❌ Transcription failed: {e}")
            if output_file.exists(): os.remove(output_file)
            # Don't delete cache dir - allows resume on retry
            raise e

    def get_text_at_time(self, transcript_path, timestamp):
        try:
            data = self._get_cached_transcript(transcript_path)
            if not data: return None

            target_idx = -1
            for i, seg in enumerate(data):
                if seg['start'] <= timestamp <= seg['end']:
                    target_idx = i
                    break
            
            if target_idx == -1:
                closest_dist = float('inf')
                for i, seg in enumerate(data):
                    dist = min(abs(timestamp - seg['start']), abs(timestamp - seg['end']))
                    if dist < closest_dist:
                        closest_dist = dist
                        target_idx = i

            if target_idx == -1: return None

            # Get surrounding context
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
                if current_len >= TARGET_LEN: break
                if right < len(data):
                    segments_indices.append(right)
                    current_len += len(data[right]['text'])
                    right += 1
                    added = True
                if not added: break

            raw_text = " ".join([data[i]['text'] for i in segments_indices])
            return self._clean_text(raw_text)

        except Exception as e:
            logger.error(f"Error reading transcript {transcript_path}: {e}")
        return None

    def find_time_for_text(self, transcript_path, search_text, hint_percentage=None):
        from rapidfuzz import fuzz
        
        try:
            data = self._get_cached_transcript(transcript_path)
            if not data: return None
            
            # CHANGED: Clean the search text immediately
            clean_search = self._clean_text(search_text)
            
            windows = []
            window_size = 12
            
            for i in range(0, len(data), window_size // 2):
                window_text = " ".join([data[j]['text'] for j in range(i, min(i + window_size, len(data)))])
                windows.append({
                    'start': data[i]['start'],
                    'end': data[min(i + window_size, len(data))-1]['end'],
                    'text': self._clean_text(window_text), # CHANGED: Clean window text
                    'index': i
                })
            
            if not windows: return None
            
            best_match = None
            best_score = 0
            
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
                    logger.debug(f"Hint match: {best_score}% at {best_match['start']:.1f}s")
                    return best_match['start']
            
            for window in windows:
                score = fuzz.token_set_ratio(clean_search, window['text'])
                if score > best_score:
                    best_score = score
                    best_match = window
            
            if best_match and best_score >= self.match_threshold:
                logger.info(f"✅ Text match found: {best_score}% (Threshold: {self.match_threshold})")
                return best_match['start']
            else:
                logger.warning(f"No good match found (best: {best_score}% < {self.match_threshold}%)")
                return None
                
        except Exception as e:
            logger.error(f"Error searching transcript {transcript_path}: {e}")
        return None
# [END FILE]
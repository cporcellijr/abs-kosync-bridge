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

logger = logging.getLogger(__name__)

class AudioTranscriber:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.transcripts_dir = data_dir / "transcripts"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.cache_root = data_dir / "audio_cache"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        
        # Model size - "tiny" is fast but less accurate
        # Options: tiny, base, small, medium, large
        self.model_size = os.environ.get("WHISPER_MODEL", "tiny")
        
        # LRU Cache for loaded transcripts
        self._transcript_cache = OrderedDict()
        self._cache_capacity = 3
        
        # Matching settings
        self.match_threshold = int(os.environ.get("TRANSCRIPT_MATCH_THRESHOLD", 85))

    def _get_cached_transcript(self, path):
        """Load transcript with LRU caching."""
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

    def get_audio_duration(self, file_path):
        """Returns the duration of the audio file in seconds using ffprobe."""
        cmd = [
            'ffprobe', 
            '-v', 'error', 
            '-show_entries', 'format=duration', 
            '-of', 'default=noprint_wrappers=1:nokey=1', 
            str(file_path)
        ]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return float(result.stdout.strip())
        except (ValueError, subprocess.CalledProcessError) as e:
            logger.error(f"Could not determine duration for {file_path}: {e}")
            return 0.0

    def split_audio_file(self, file_path, target_max_duration_sec=2700):
        """
        Checks if file exceeds max duration. If so, splits it into even chunks.
        Default: 2700s = 45 mins
        """
        duration = self.get_audio_duration(file_path)
        
        if duration <= target_max_duration_sec:
            return [file_path]

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
                'ffmpeg', '-y',
                '-i', str(file_path),
                '-ss', str(start_time),
                '-t', str(segment_duration),
                '-map', '0:a',
                '-c', 'copy',  
                '-loglevel', 'error',
                str(new_path)
            ]
            
            subprocess.run(cmd, check=True)
            new_files.append(new_path)
            logger.info(f"  Created chunk {i+1}/{num_parts}: {new_filename}")

        file_path.unlink() 
        return new_files

    def process_audio(self, abs_id, audio_urls):
        """Transcribe audiobook and save to JSON."""
        output_file = self.transcripts_dir / f"{abs_id}.json"
        
        if output_file.exists():
            logger.info(f"Transcript already exists for {abs_id}")
            return output_file

        book_cache_dir = self.cache_root / str(abs_id)
        if book_cache_dir.exists():
            shutil.rmtree(book_cache_dir)
        book_cache_dir.mkdir(parents=True, exist_ok=True)

        downloaded_files = []
        MAX_DURATION_SECONDS = 45 * 60

        try:
            # --- PHASE 1: DOWNLOAD ---
            logger.info(f"📥 Phase 1: Downloading {len(audio_urls)} audio files...")
            
            for idx, audio_data in enumerate(audio_urls):
                stream_url = audio_data['stream_url']
                extension = audio_data.get('ext', '.mp3')
                local_filename = f"part_{idx:03d}{extension}"
                local_path = book_cache_dir / local_filename
                
                logger.info(f"   Downloading Part {idx + 1}/{len(audio_urls)}...")

                try:
                    with requests.get(stream_url, stream=True, timeout=120) as r:
                        r.raise_for_status()
                        with open(local_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                    
                    if not local_path.exists() or local_path.stat().st_size == 0:
                        raise ValueError(f"File {local_path} is empty or missing.")

                    final_parts = self.split_audio_file(local_path, MAX_DURATION_SECONDS)
                    downloaded_files.extend(final_parts)
                    
                except Exception as e:
                    logger.error(f"❌ Failed to download Part {idx + 1}: {e}")
                    raise e

            logger.info(f"✅ All parts cached. Starting transcription...")

            # --- PHASE 2: TRANSCRIBE ---
            logger.info(f"🧠 Phase 2: Transcribing using {self.model_size} model...")
            
            model = WhisperModel(self.model_size, device="cpu", compute_type="int8", cpu_threads=4)
            full_transcript = []
            cumulative_duration = 0.0

            for idx, local_path in enumerate(downloaded_files):
                duration = self.get_audio_duration(local_path)
                logger.info(f"   Transcribing Part {idx + 1}/{len(downloaded_files)} ({duration:.0f}s)...")
                
                segments, info = model.transcribe(str(local_path), beam_size=1, best_of=1)
                
                for segment in segments:
                    full_transcript.append({
                        "start": segment.start + cumulative_duration,
                        "end": segment.end + cumulative_duration,
                        "text": segment.text.strip()
                    })
                
                cumulative_duration += duration
                gc.collect()

            # --- PHASE 3: SAVE ---
            with open(output_file, 'w') as f:
                json.dump(full_transcript, f)
            
            logger.info(f"✅ Transcription complete: {len(full_transcript)} segments")
            return output_file

        except Exception as e:
            logger.error(f"❌ Transcription failed: {e}")
            if output_file.exists():
                os.remove(output_file)
            raise e
            
        finally:
            if book_cache_dir.exists():
                logger.info("🧹 Cleaning up cache...")
                shutil.rmtree(book_cache_dir)

    def get_text_at_time(self, transcript_path, timestamp):
        """
        Get ~800 chars of transcript text centered around a timestamp.
        Used for ABS → ebook sync.
        """
        try:
            data = self._get_cached_transcript(transcript_path)
            if not data: return None

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

            if target_idx == -1: return None

            # Expand to ~800 chars for better matching
            TARGET_LEN = 800
            segments_indices = [target_idx]
            current_len = len(data[target_idx]['text'])
            left = target_idx - 1
            right = target_idx + 1
            
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

            return " ".join([data[i]['text'] for i in segments_indices])

        except Exception as e:
            logger.error(f"Error reading transcript {transcript_path}: {e}")
        
        return None

    def find_time_for_text(self, transcript_path, search_text, hint_percentage=None):
        """
        Find the timestamp in the transcript that best matches the search text.
        Used for ebook → ABS sync.
        
        FIXED: Uses sliding window to match ~500 char chunks instead of
        individual segments. This prevents false matches on common phrases.
        
        Args:
            transcript_path: Path to transcript JSON
            search_text: Text from ebook (~400-800 chars)
            hint_percentage: Optional hint for where to search first (0.0-1.0)
        """
        from rapidfuzz import fuzz
        
        try:
            data = self._get_cached_transcript(transcript_path)
            if not data or len(data) == 0: 
                return None
            
            # Build sliding windows of ~500 chars each
            # Each window combines multiple consecutive segments
            windows = []
            window_size = 12  # ~12 segments ≈ 400-600 chars typically
            
            for i in range(0, len(data), window_size // 2):  # 50% overlap
                window_text = ""
                window_start = data[i]['start']
                window_end = data[i]['end']
                
                for j in range(i, min(i + window_size, len(data))):
                    window_text += " " + data[j]['text']
                    window_end = data[j]['end']
                
                windows.append({
                    'start': window_start,
                    'end': window_end,
                    'text': window_text.strip(),
                    'index': i
                })
            
            if not windows:
                return None
            
            # If we have a hint, search nearby first (±15% of book)
            best_match = None
            best_score = 0
            
            if hint_percentage is not None:
                # Calculate expected window index based on hint
                total_duration = data[-1]['end']
                hint_time = hint_percentage * total_duration
                
                # Find windows within ±15% of hint
                hint_start = max(0, hint_percentage - 0.15) * total_duration
                hint_end = min(1.0, hint_percentage + 0.15) * total_duration
                
                nearby_windows = [w for w in windows if w['start'] >= hint_start and w['start'] <= hint_end]
                
                for window in nearby_windows:
                    score = fuzz.token_set_ratio(search_text.lower(), window['text'].lower())
                    if score > best_score:
                        best_score = score
                        best_match = window
                
                # If we found a good match nearby, use it
                if best_score >= self.match_threshold:
                    logger.debug(f"Hint match: {best_score}% at {best_match['start']:.1f}s")
                    return best_match['start']
            
            # Full search if no hint or hint didn't find good match
            for window in windows:
                score = fuzz.token_set_ratio(search_text.lower(), window['text'].lower())
                if score > best_score:
                    best_score = score
                    best_match = window
            
            if best_match and best_score >= self.match_threshold:
                logger.debug(f"Text match: {best_score}% at {best_match['start']:.1f}s")
                return best_match['start']
            else:
                logger.warning(f"No good match found (best: {best_score}%)")
                return None
                
        except Exception as e:
            logger.error(f"Error searching transcript {transcript_path}: {e}")
        
        return None
# [END FILE]

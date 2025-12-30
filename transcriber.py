import json
import logging
import os
import shutil
import subprocess
import gc
from pathlib import Path
from faster_whisper import WhisperModel
import requests
import ffmpeg
import math

logger = logging.getLogger(__name__)

class AudioTranscriber:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.transcripts_dir = data_dir / "transcripts"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.cache_root = data_dir / "audio_cache"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.model_size = "tiny" 

    def _get_audio_duration(self, filepath):
        try:
            cmd = [
                "ffprobe", 
                "-v", "error", 
                "-show_entries", "format=duration", 
                "-of", "default=noprint_wrappers=1:nokey=1", 
                str(filepath)
            ]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            return float(result.stdout.strip())
        except Exception as e:
            logger.error(f"Failed to get duration for {filepath}: {e}")
            return 0.0

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
        except (ValueError, subprocess.CalledProcessError):
            logger.error(f"Could not determine duration for {file_path}")
            return 0.0

    ## NOTE TO FUTURE ME - 45mins is hard coded - add to environment variables for user vconfiguration
    def split_audio_file(self, file_path, target_max_duration_sec=2700): # 2700s = 45 mins
        """
        Checks if file exceeds max duration. If so, splits it into even chunks 
        and returns list of new paths. If not, returns list containing original path.
        """
        duration = self.get_audio_duration(file_path)
        
        # If file is within limits, return original
        if duration <= target_max_duration_sec:
            return [file_path]

        logger.info(f"⚠️ File {file_path.name} is {duration/60:.2f}m. Splitting to avoid RAM overload...")

        # Calculate how many parts we need to keep them under the limit
        num_parts = math.ceil(duration / target_max_duration_sec)
        
        # Calculate the exact duration for each even chunk
        segment_duration = duration / num_parts
        
        new_files = []
        base_name = file_path.stem
        extension = file_path.suffix

        for i in range(num_parts):
            start_time = i * segment_duration
            new_filename = f"{base_name}_split_{i+1:03d}{extension}"
            new_path = file_path.parent / new_filename
            
            # FFmpeg command to slice audio
            # -ss : Start time
            # -t  : Duration of the clip
            # -map 0:a : Only copy audio streams (ignore cover art/metadata)
            # -c copy : Stream copy (FAST, no re-encoding, low RAM)
            # Note: If precise cutting is required and -c copy is inaccurate, 
            # remove '-c', 'copy' to re-encode (slower but precise).
            cmd = [
                'ffmpeg', '-y',
                '-i', str(file_path),
                '-ss', str(start_time),
                '-t', str(segment_duration),
                '-map', '0:a',  # Only select audio streams
                '-c', 'copy',  
                '-loglevel', 'error',
                str(new_path)
            ]
            
            subprocess.run(cmd, check=True)
            new_files.append(new_path)
            logger.info(f"  Created chunk {i+1}/{num_parts}: {new_filename} ({segment_duration/60:.2f}m)")

        # Delete the original large file to save disk space
        file_path.unlink() 
        
        return new_files

    def process_audio(self, abs_id, audio_urls):
        output_file = self.transcripts_dir / f"{abs_id}.json"
        
        if output_file.exists():
            logger.info(f"Transcript already exists for {abs_id}")
            return output_file

        book_cache_dir = self.cache_root / str(abs_id)
        if book_cache_dir.exists():
            shutil.rmtree(book_cache_dir)
        book_cache_dir.mkdir(parents=True, exist_ok=True)

        downloaded_files = []
        ## Note to future me - this hard coded!
        MAX_DURATION_SECONDS = 45 * 60  # 45 minutes

        try:
            # --- PHASE 1: DOWNLOAD ---
            logger.info(f"📥 Phase 1: Caching {len(audio_urls)} audio parts locally for {abs_id}...")
            
            for idx, audio_data in enumerate(audio_urls):
                stream_url = audio_data['stream_url']
                extension = audio_data.get('ext', '.mp3')
                local_filename_old = f"part_{idx:03d}.mp3"
                local_filename = f"part_{idx:03d}{extension}"
                logger.debug(f"NEW {local_filename} -- {local_filename_old}")
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

                    # --- PHASE 1.5: Identify if audio exceeds 45min limit and chunk if necessary ---
                    # Check length and split if necessary before appending
                    final_parts = self.split_audio_file(local_path, MAX_DURATION_SECONDS)
                    
                    downloaded_files.extend(final_parts)
                    
                except Exception as e:
                    logger.error(f"❌ Failed to download Part {idx + 1}: {e}")
                    raise e

            logger.info(f"✅ All parts cached. Starting AI processing...")

            # --- PHASE 2: TRANSCRIBE ---
            logger.info(f"🧠 Phase 2: Transcribing using {self.model_size} model...")
            
            # Optimization: cpu_threads set explicitly, compute_type int8
            model = WhisperModel(self.model_size, device="cpu", compute_type="int8", cpu_threads=4)
            full_transcript = []
            cumulative_duration = 0.0

            for idx, local_path in enumerate(downloaded_files):
                duration = self._get_audio_duration(local_path)
                logger.info(f"   Transcribing Part {idx + 1}/{len(downloaded_files)} (Length: {duration:.2f}s)...")
                
                # CRITICAL FIX: beam_size=1 (Greedy Search) prevents OOM on long files
                segments, info = model.transcribe(str(local_path), beam_size=1, best_of=1)
                
                for segment in segments:
                    full_transcript.append({
                        "start": segment.start + cumulative_duration,
                        "end": segment.end + cumulative_duration,
                        "text": segment.text.strip()
                    })
                
                cumulative_duration += duration
                
                # Optimization: Force garbage collection after each part
                gc.collect()

            # --- PHASE 3: SAVE ---
            with open(output_file, 'w') as f:
                json.dump(full_transcript, f)
            
            logger.info(f"✅ Full transcription complete. Saved to: {output_file}")
            return output_file

        except Exception as e:
            logger.error(f"❌ Critical Failure during processing: {e}")
            if output_file.exists():
                os.remove(output_file)
            raise e
            
        finally:
            if book_cache_dir.exists():
                logger.info("🧹 Cleaning up audio cache...")
                shutil.rmtree(book_cache_dir)

    def get_text_at_time(self, transcript_path, timestamp):
        try:
            with open(transcript_path, 'r') as f:
                data = json.load(f)

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

            TARGET_LEN = 400
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

    def find_time_for_text(self, transcript_path, search_text):
        from rapidfuzz import process, fuzz
        try:
            with open(transcript_path, 'r') as f:
                data = json.load(f)
            
            texts = [d['text'] for d in data]
            match = process.extractOne(search_text, texts, scorer=fuzz.partial_ratio)
            
            if match and match[1] > 80:
                index = match[2]
                return data[index]['start']
        except Exception as e:
            logger.error(f"Error searching transcript {transcript_path}: {e}")
        
        return None

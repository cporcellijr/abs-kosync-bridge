# [START FILE: src/utils/smil_extractor.py]
import json
import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from typing import Optional, List, Dict
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

class SmilExtractor:
    """
    Extracts transcript data from EPUB3 media overlays.
    Hybrid Version: Uses V1 Regex (Proven) + V2 Alignment (Feature).
    """
    
    def __init__(self):
        self._xhtml_cache = {}

    def has_media_overlays(self, epub_path: str) -> bool:
        try:
            with zipfile.ZipFile(epub_path, 'r') as zf:
                opf_path = self._find_opf_path(zf)
                if not opf_path: return False
                
                opf_content = zf.read(opf_path).decode('utf-8')
                root = ET.fromstring(opf_content)
                manifest = root.find('.//{http://www.idpf.org/2007/opf}manifest')
                if manifest is None: return False
                
                for item in manifest.findall('{http://www.idpf.org/2007/opf}item'):
                    if item.get('media-type') == 'application/smil+xml':
                        return True
                return False
        except Exception as e:
            logger.debug(f"Error checking media overlays: {e}")
            return False

    def extract_transcript(self, epub_path: str, abs_chapters: List[Dict] = None, audio_offset: float = 0.0) -> List[Dict]:
        transcript = []
        self._xhtml_cache = {}
        
        try:
            with zipfile.ZipFile(epub_path, 'r') as zf:
                opf_path = self._find_opf_path(zf)
                if not opf_path:
                    logger.error(f"Could not find OPF file in EPUB: {epub_path}")
                    return []
                
                opf_dir = str(Path(opf_path).parent)
                if opf_dir == '.': opf_dir = ''
                
                opf_content = zf.read(opf_path).decode('utf-8')
                smil_files = self._get_smil_files_in_order(opf_content, opf_dir, zf)
                
                if not smil_files:
                    logger.warning(f"No SMIL files found in EPUB: {epub_path}")
                    return []
                
                logger.info(f"📖 Found {len(smil_files)} SMIL files in EPUB")
                
                # --- AUTO ALIGNMENT ---
                chapter_map = {}
                if abs_chapters:
                    chapter_map = self._align_chapters(zf, smil_files, abs_chapters)
                # ----------------------

                current_offset = audio_offset
                
                for idx, smil_path in enumerate(smil_files):
                    file_offset = current_offset
                    abs_title_log = f"Loop #{idx+1}"

                    if idx in chapter_map:
                        mapped_chap = chapter_map[idx]
                        file_offset = float(mapped_chap.get('start', 0))
                        abs_title = mapped_chap.get('title', f"Chapter {idx+1}")
                        abs_title_log = f"'{abs_title}'"
                    
                    # LOGGING UPDATE: Shows the actual ABS title it mapped to
                    if idx < 5: # Only log first 5 to reduce spam
                        logger.debug(f"   Mapping {Path(smil_path).name} -> ABS {abs_title_log} (Start: {file_offset:.2f}s)")
                    
                    segments = self._process_smil_file(zf, smil_path, file_offset)
                    transcript.extend(segments)
                    
                    if not chapter_map and segments:
                        max_end = max(s['end'] for s in segments)
                        current_offset = max(current_offset, max_end)
                
                transcript.sort(key=lambda x: x['start'])
                logger.info(f"✅ Extracted {len(transcript)} segments from SMIL")
                return transcript
                
        except Exception as e:
            logger.error(f"Error extracting SMIL transcript: {e}")
            return []

    def _align_chapters(self, zf, smil_files, abs_chapters) -> Dict[int, Dict]:
        """Maps SMIL file indices to ABS Chapters by comparing Durations."""
        if not abs_chapters or not smil_files: return {}

        smil_durations = []
        for path in smil_files:
            dur = 0.0
            try:
                content = zf.read(path).decode('utf-8')
                clips = re.findall(r'clipEnd=["\']([^"\']+)["\']', content)
                if clips:
                    dur = self._parse_timestamp(clips[-1])
            except: pass
            smil_durations.append(dur)

        abs_durations = []
        for ch in abs_chapters:
            start = float(ch.get('start', 0))
            end = float(ch.get('end', 0))
            abs_durations.append(end - start)

        best_offset = 0
        min_error = float('inf')

        # Check shifts from 0 to 2
        for offset in range(0, 3): 
            error = 0.0
            matches = 0
            for smil_idx, smil_dur in enumerate(smil_durations):
                abs_idx = smil_idx + offset
                if abs_idx < len(abs_durations):
                    abs_dur = abs_durations[abs_idx]
                    diff = abs(smil_dur - abs_dur)
                    if abs_dur > 0:
                        error += diff
                        matches += 1
            if matches > 0:
                avg_error = error / matches
                if avg_error < min_error:
                    min_error = avg_error
                    best_offset = offset

        logger.info(f"🧩 Auto-Aligned Chapters: Offset={best_offset} (Avg Error: {min_error:.2f}s)")
        
        mapping = {}
        for smil_idx in range(len(smil_files)):
            abs_idx = smil_idx + best_offset
            if 0 <= abs_idx < len(abs_chapters):
                mapping[smil_idx] = abs_chapters[abs_idx]
        return mapping

    def _process_smil_file(self, zf, smil_path, audio_offset):
        segments = []
        try:
            smil_content = zf.read(smil_path).decode('utf-8')
            smil_dir = str(Path(smil_path).parent)
            if smil_dir == '.': smil_dir = ''
            
            smil_content = re.sub(r'xmlns="[^"]+"', '', smil_content)
            smil_content = re.sub(r'xmlns:[a-z]+="[^"]+"', '', smil_content)
            smil_content = re.sub(r'epub:', '', smil_content)
            
            root = ET.fromstring(smil_content)
            
            for par in root.iter('par'):
                segment = self._parse_par_element(par, zf, smil_dir, audio_offset)
                if segment:
                    segments.append(segment)
        except Exception as e:
            logger.warning(f"Error processing SMIL {smil_path}: {e}")
        return segments

    def _parse_par_element(self, par, zf, smil_dir, audio_offset):
        text_elem = par.find('text')
        audio_elem = par.find('audio')
        
        if text_elem is None or audio_elem is None: return None
        
        clip_begin = self._parse_timestamp(audio_elem.get('clipBegin', '0s'))
        clip_end = self._parse_timestamp(audio_elem.get('clipEnd', '0s'))
        
        start_time = clip_begin + audio_offset
        end_time = clip_end + audio_offset
        
        text_src = text_elem.get('src', '')
        text_content = self._get_text_content(zf, smil_dir, text_src)
        
        if not text_content: return None
        
        return {
            'start': round(start_time, 3),
            'end': round(end_time, 3),
            'text': text_content
        }

    # --- Helpers ---

    def _find_opf_path(self, zf: zipfile.ZipFile) -> Optional[str]:
        try:
            container = zf.read('META-INF/container.xml').decode('utf-8')
            root = ET.fromstring(container)
            for rootfile in root.iter():
                if rootfile.tag.endswith('rootfile'):
                    return rootfile.get('full-path')
        except: pass
        return None

    def _natural_sort_key(self, s):
        return [int(text) if text.isdigit() else text.lower()
                for text in re.split(r'(\d+)', s)]

    def _get_smil_files_in_order(self, opf_content: str, opf_dir: str, zf: zipfile.ZipFile) -> List[str]:
        root = ET.fromstring(opf_content)
        manifest = root.find('.//{http://www.idpf.org/2007/opf}manifest')
        spine = root.find('.//{http://www.idpf.org/2007/opf}spine')
        if manifest is None: return []
        
        smil_items = {} 
        content_to_overlay = {} 
        
        for item in manifest.findall('{http://www.idpf.org/2007/opf}item'):
            if item.get('media-type') == 'application/smil+xml':
                smil_items[item.get('id')] = item.get('href')
            if item.get('media-overlay'):
                content_to_overlay[item.get('id')] = item.get('media-overlay')
        
        smil_files = []
        seen = set()
        
        if spine is not None:
            for itemref in spine.findall('{http://www.idpf.org/2007/opf}itemref'):
                idref = itemref.get('idref')
                if idref in content_to_overlay:
                    overlay_id = content_to_overlay[idref]
                    if overlay_id in smil_items and overlay_id not in seen:
                        path = self._resolve_path(opf_dir, smil_items[overlay_id])
                        smil_files.append(path)
                        seen.add(overlay_id)

        if not smil_files and smil_items:
            logger.warning("⚠️ Spine media-overlay lookup failed, falling back to natural sort")
            all_smil = [self._resolve_path(opf_dir, href) for href in smil_items.values()]
            smil_files = sorted(all_smil, key=self._natural_sort_key)
        
        valid_files = []
        for path in smil_files:
            for variant in [path, path.lstrip('/'), path.replace('\\', '/')]:
                try:
                    zf.getinfo(variant)
                    valid_files.append(variant)
                    break
                except KeyError: continue
        return valid_files

    def _resolve_path(self, base, rel):
        if not base: return rel
        return str(Path(base) / rel).replace('\\', '/')

    def _parse_timestamp(self, ts_str: str) -> float:
        if not ts_str: return 0.0
        ts_str = ts_str.strip().replace('s', '')
        if ':' in ts_str:
            parts = ts_str.split(':')
            return sum(float(p) * (60 ** i) for i, p in enumerate(reversed(parts)))
        try: return float(ts_str)
        except ValueError: return 0.0

    def _get_text_content(self, zf, smil_dir, text_src):
        if not text_src: return None
        if '#' in text_src: file_path, fragment_id = text_src.split('#', 1)
        else: file_path, fragment_id = text_src, None
        
        full_path = self._resolve_path(smil_dir, file_path)
        if full_path not in self._xhtml_cache:
            for variant in [full_path, full_path.lstrip('/'), full_path.replace('\\', '/')]:
                try:
                    content = zf.read(variant).decode('utf-8')
                    self._xhtml_cache[full_path] = BeautifulSoup(content, 'html.parser')
                    break
                except KeyError: continue
        
        soup = self._xhtml_cache.get(full_path)
        if not soup: return None
        
        if fragment_id:
            element = soup.find(id=fragment_id)
            if element:
                text = element.get_text(separator=' ', strip=True)
                return re.sub(r'\s+', ' ', text).strip()
        
        return None

def extract_transcript_from_epub(epub_path: str, abs_chapters: List[Dict] = None, 
                               output_path: str = None) -> Optional[str]:
    extractor = SmilExtractor()
    if not extractor.has_media_overlays(epub_path): return None
    
    transcript = extractor.extract_transcript(epub_path, abs_chapters)
    if not transcript: return None
    
    if output_path is None:
        output_path = str(Path(epub_path).with_suffix('.transcript.json'))
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(transcript, f, ensure_ascii=False)
    
    return output_path

if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python smil_extractor.py <epub_file>")
        sys.exit(1)
    extract_transcript_from_epub(sys.argv[1])
# [END FILE]
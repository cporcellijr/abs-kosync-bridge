# [START FILE: abs-kosync-enhanced/smil_extractor.py]


import json
import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from typing import Optional, List, Dict, Tuple
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class SmilExtractor:
 
    
    def __init__(self):
        self._xhtml_cache = {}  # Cache parsed XHTML files within a single extraction
    
    def has_media_overlays(self, epub_path: str) -> bool:
        """
        Check if an EPUB has media overlay (SMIL) files.
        """
        try:
            with zipfile.ZipFile(epub_path, 'r') as zf:
                # Find the OPF file
                opf_path = self._find_opf_path(zf)
                if not opf_path:
                    return False
                
                # Parse OPF and look for SMIL items in manifest
                opf_content = zf.read(opf_path).decode('utf-8')
                root = ET.fromstring(opf_content)
                
                # Look for media-type="application/smil+xml" in manifest
                manifest = root.find('.//{http://www.idpf.org/2007/opf}manifest')
                if manifest is None:
                    return False
                
                for item in manifest.findall('{http://www.idpf.org/2007/opf}item'):
                    if item.get('media-type') == 'application/smil+xml':
                        return True
                
                return False
                
        except Exception as e:
            logger.debug(f"Error checking for media overlays in {epub_path}: {e}")
            return False
    
    def extract_transcript(self, epub_path: str, abs_chapters: List[Dict] = None, 
                          audio_offset: float = 0.0) -> List[Dict]:
        """
        Extract transcript from EPUB3 media overlays.
        
        Args:
            epub_path: Path to the EPUB file
            abs_chapters: List of dicts from ABS metadata [{"id":0, "start":0, ...}]
            audio_offset: Global offset to add (fallback if abs_chapters missing)
                           
        Returns:
            List of transcript segments: [{"start": float, "end": float, "text": str}, ...]
        """
        transcript = []
        self._xhtml_cache = {}  # Clear cache for new extraction
        
        try:
            with zipfile.ZipFile(epub_path, 'r') as zf:
                # Find OPF and get SMIL files in spine order
                opf_path = self._find_opf_path(zf)
                if not opf_path:
                    logger.error(f"Could not find OPF file in EPUB: {epub_path}")
                    return []
                
                opf_dir = str(Path(opf_path).parent)
                if opf_dir == '.':
                    opf_dir = ''
                    
                opf_content = zf.read(opf_path).decode('utf-8')
                
                # Get ordered list of SMIL files
                smil_files = self._get_smil_files_in_order(opf_content, opf_dir, zf)
                
                if not smil_files:
                    logger.warning(f"No SMIL files found in EPUB: {epub_path}")
                    return []
                
                logger.info(f"📖 Found {len(smil_files)} SMIL files in {Path(epub_path).name}")
                
                # Process each SMIL file
                current_offset = audio_offset
                
                for idx, smil_path in enumerate(smil_files):
                    file_offset = current_offset

                    # INTELLIGENT MAPPING: Map SMIL file to ABS Chapter
                    # If we have chapter data, force the offset to match the ABS chapter start.
                    if abs_chapters and idx < len(abs_chapters):
                        # Match by index (most reliable for Storyteller output)
                        chapter = abs_chapters[idx]
                        file_offset = float(chapter.get('start', 0))
                        logger.debug(f"   Mapping {Path(smil_path).name} -> Chapter {idx+1} (Start: {file_offset:.2f}s)")
                    
                    segments = self._process_smil_file(zf, smil_path, file_offset)
                    transcript.extend(segments)
                    
                    # If we don't have ABS chapters, accumulate offset flow
                    if not abs_chapters and segments:
                        max_end = max(s['end'] for s in segments)
                        current_offset = max(current_offset, max_end)
                
                # Sort by start time (should already be sorted, but ensure it)
                transcript.sort(key=lambda x: x['start'])
                
                logger.info(f"✅ Extracted {len(transcript)} segments from SMIL")
                
        except zipfile.BadZipFile:
            logger.error(f"Invalid EPUB file: {epub_path}")
        except Exception as e:
            logger.error(f"Error extracting transcript from {epub_path}: {e}")
        
        return transcript
    
    def _find_opf_path(self, zf: zipfile.ZipFile) -> Optional[str]:
        """Find the OPF file path from container.xml"""
        try:
            container = zf.read('META-INF/container.xml').decode('utf-8')
            root = ET.fromstring(container)
            
            # Find rootfile element
            for rootfile in root.iter():
                if rootfile.tag.endswith('rootfile'):
                    return rootfile.get('full-path')
            
            return None
        except Exception as e:
            logger.debug(f"Error finding OPF: {e}")
            return None
    
    def _natural_sort_key(self, s):
        """Key for natural sorting (handles numbers correctly: 2 before 10)"""
        return [int(text) if text.isdigit() else text.lower()
                for text in re.split(r'(\d+)', s)]

    def _get_smil_files_in_order(self, opf_content: str, opf_dir: str, zf: zipfile.ZipFile) -> List[str]:
        root = ET.fromstring(opf_content)
        
        # 1. Parse Manifest
        manifest_elem = root.find('.//{http://www.idpf.org/2007/opf}manifest')
        if manifest_elem is None: return []
        
        smil_items = {}
        content_to_overlay = {}
        
        for item in manifest_elem.findall('{http://www.idpf.org/2007/opf}item'):
            item_id = item.get('id')
            href = item.get('href')
            media_type = item.get('media-type')
            media_overlay = item.get('media-overlay')
            
            if media_type == 'application/smil+xml':
                smil_items[item_id] = href
            if media_overlay:
                content_to_overlay[item_id] = media_overlay
        
        # 2. Parse Spine to get order
        spine = root.find('.//{http://www.idpf.org/2007/opf}spine')
        smil_files = []
        seen_smil = set()
        
        if spine is not None:
            for itemref in spine.findall('{http://www.idpf.org/2007/opf}itemref'):
                idref = itemref.get('idref')
                if idref in content_to_overlay:
                    smil_id = content_to_overlay[idref]
                    if smil_id in smil_items and smil_id not in seen_smil:
                        smil_path = self._resolve_path(opf_dir, smil_items[smil_id])
                        smil_files.append(smil_path)
                        seen_smil.add(smil_id)
        
        # 3. Fallback: Natural Sort if spine lookup failed
        if not smil_files and smil_items:
            logger.info("⚠️ Spine media-overlay lookup failed, falling back to natural sort")
            all_smil = [self._resolve_path(opf_dir, href) for href in smil_items.values()]
            smil_files = sorted(all_smil, key=self._natural_sort_key)
        
        # 4. Validate existence in zip
        valid_files = []
        for smil_path in smil_files:
            # Try variations for path separators
            for path_variant in [smil_path, smil_path.lstrip('/'), smil_path.replace('\\', '/')]:
                try:
                    zf.getinfo(path_variant)
                    valid_files.append(path_variant)
                    break
                except KeyError:
                    continue
        return valid_files
    
    def _resolve_path(self, base_dir: str, relative_path: str) -> str:
        """Resolve a relative path against a base directory."""
        if not base_dir:
            return relative_path
        
        full = str(Path(base_dir) / relative_path)
        parts = []
        for part in full.replace('\\', '/').split('/'):
            if part == '..':
                if parts: parts.pop()
            elif part and part != '.':
                parts.append(part)
        
        return '/'.join(parts)
    
    def _process_smil_file(self, zf: zipfile.ZipFile, smil_path: str,
                          audio_offset: float) -> List[Dict]:
        """
        Process a single SMIL file and extract transcript segments.
        """
        segments = []
        try:
            smil_content = zf.read(smil_path).decode('utf-8')
            smil_dir = str(Path(smil_path).parent)
            if smil_dir == '.': smil_dir = ''
            
            # Clean namespaces
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
    
    def _parse_par_element(self, par: ET.Element, zf: zipfile.ZipFile,
                          smil_dir: str, audio_offset: float) -> Optional[Dict]:
        """
        Parse a <par> element containing text and audio references.
        """
        text_elem = par.find('text')
        audio_elem = par.find('audio')
        
        if text_elem is None or audio_elem is None:
            return None
        
        clip_begin = self._parse_timestamp(audio_elem.get('clipBegin', '0s'))
        clip_end = self._parse_timestamp(audio_elem.get('clipEnd', '0s'))
        
        # Apply ABS Chapter Offset
        start_time = clip_begin + audio_offset
        end_time = clip_end + audio_offset
        
        text_src = text_elem.get('src', '')
        text_content = self._get_text_content(zf, smil_dir, text_src)
        
        if not text_content:
            return None
        
        return {
            'start': round(start_time, 3),
            'end': round(end_time, 3),
            'text': text_content
        }
    
    def _parse_timestamp(self, ts_str: str) -> float:
        if not ts_str: return 0.0
        ts_str = ts_str.strip().replace('s', '')
        
        if ':' in ts_str:
            parts = ts_str.split(':')
            seconds = 0.0
            for i, part in enumerate(reversed(parts)):
                seconds += float(part) * (60 ** i)
            return seconds
        try:
            return float(ts_str)
        except ValueError:
            return 0.0
    
    def _get_text_content(self, zf: zipfile.ZipFile, smil_dir: str, 
                         text_src: str) -> Optional[str]:
        if not text_src: return None
        
        if '#' in text_src:
            file_path, fragment_id = text_src.split('#', 1)
        else:
            file_path = text_src
            fragment_id = None
        
        full_path = self._resolve_path(smil_dir, file_path)
        
        if full_path not in self._xhtml_cache:
            soup = self._load_xhtml(zf, full_path)
            if soup:
                self._xhtml_cache[full_path] = soup
            else:
                return None
        
        soup = self._xhtml_cache.get(full_path)
        if not soup: return None
        
        if fragment_id:
            element = soup.find(id=fragment_id)
            if element:
                text = element.get_text(separator=' ', strip=True)
                return re.sub(r'\s+', ' ', text).strip()
        
        return None
    
    def _load_xhtml(self, zf: zipfile.ZipFile, path: str) -> Optional[BeautifulSoup]:
        for path_variant in [path, path.lstrip('/'), path.replace('\\', '/')]:
            try:
                content = zf.read(path_variant).decode('utf-8')
                return BeautifulSoup(content, 'html.parser')
            except KeyError:
                continue
        return None

def extract_transcript_from_epub(epub_path: str, abs_chapters: List[Dict] = None, 
                               output_path: str = None) -> Optional[str]:
    """Convenience function."""
    extractor = SmilExtractor()
    
    if not extractor.has_media_overlays(epub_path):
        return None
    
    transcript = extractor.extract_transcript(epub_path, abs_chapters)
    
    if not transcript:
        return None
    
    if output_path is None:
        output_path = str(Path(epub_path).with_suffix('.transcript.json'))
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(transcript, f, ensure_ascii=False)
    
    return output_path
# [END FILE]
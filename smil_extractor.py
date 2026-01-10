"""
SMIL Media Overlay Extractor for ABS-KoSync Enhanced

Extracts transcript data from EPUB3 media overlay (SMIL) files.
This bypasses Whisper transcription for books already processed by Storyteller.

Output format matches Whisper transcript JSON:
[
    {"start": 55.603, "end": 58.857, "text": "The actual sentence text"},
    ...
]
"""

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
    """
    Extracts transcript data from EPUB3 media overlays.
    
    Storyteller creates EPUB3 files with SMIL media overlays that map
    audio timestamps to text elements. This extractor parses those
    mappings to create transcript JSON compatible with Whisper output.
    """
    
    def __init__(self):
        self._xhtml_cache = {}  # Cache parsed XHTML files within a single extraction
    
    def has_media_overlays(self, epub_path: str) -> bool:
        """
        Check if an EPUB has media overlay (SMIL) files.
        
        Args:
            epub_path: Path to the EPUB file
            
        Returns:
            True if EPUB contains SMIL media overlays
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
    
    def extract_transcript(self, epub_path: str, audio_offset: float = 0.0) -> List[Dict]:
        """
        Extract transcript from EPUB3 media overlays.
        
        Args:
            epub_path: Path to the EPUB file
            audio_offset: Offset in seconds to add to all timestamps
                         (use if EPUB audio doesn't start at 0 in ABS)
                           
        Returns:
            List of transcript segments: [{"start": float, "end": float, "text": str}, ...]
            Empty list if extraction fails
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
                
                for smil_path in smil_files:
                    # Pass the accumulated offset to the current file
                    segments = self._process_smil_file(zf, smil_path, current_offset)
                    transcript.extend(segments)
                    
                    # If we found segments, update the offset for the next file
                    # so it starts where this one ended
                    if segments:
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
    
    def _get_smil_files_in_order(self, opf_content: str, opf_dir: str, 
                                  zf: zipfile.ZipFile) -> List[str]:
        """
        Get SMIL files in spine order.
        
        EPUB spine defines reading order. Each spine item may have a
        media-overlay attribute pointing to its SMIL file.
        """
        root = ET.fromstring(opf_content)
        
        # Build manifest lookup
        manifest_elem = root.find('.//{http://www.idpf.org/2007/opf}manifest')
        if manifest_elem is None:
            return []
        
        smil_items = {}  # id -> href for SMIL files
        content_to_overlay = {}  # content id -> media-overlay id
        
        for item in manifest_elem.findall('{http://www.idpf.org/2007/opf}item'):
            item_id = item.get('id')
            href = item.get('href')
            media_type = item.get('media-type')
            media_overlay = item.get('media-overlay')
            
            if media_type == 'application/smil+xml':
                smil_items[item_id] = href
            
            if media_overlay:
                content_to_overlay[item_id] = media_overlay
        
        # Walk spine and collect SMIL files in order
        spine = root.find('.//{http://www.idpf.org/2007/opf}spine')
        smil_files = []
        seen_smil = set()
        
        if spine is not None:
            for itemref in spine.findall('{http://www.idpf.org/2007/opf}itemref'):
                idref = itemref.get('idref')
                
                # Check if this spine item has a media-overlay
                if idref in content_to_overlay:
                    smil_id = content_to_overlay[idref]
                    if smil_id in smil_items and smil_id not in seen_smil:
                        smil_path = self._resolve_path(opf_dir, smil_items[smil_id])
                        smil_files.append(smil_path)
                        seen_smil.add(smil_id)
        
        # If we didn't find any via spine, fall back to all SMIL files sorted by name
        if not smil_files:
            all_smil = [self._resolve_path(opf_dir, href) for href in smil_items.values()]
            smil_files = sorted(all_smil)
        
        # Verify files exist in ZIP
        valid_files = []
        for smil_path in smil_files:
            # Try a few path variations
            for path_variant in [smil_path, smil_path.lstrip('/'), 
                                smil_path.replace('\\', '/')]:
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
        
        # Handle ../ in paths
        full = str(Path(base_dir) / relative_path)
        
        # Normalize (resolve ..)
        parts = []
        for part in full.replace('\\', '/').split('/'):
            if part == '..':
                if parts:
                    parts.pop()
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
            if smil_dir == '.':
                smil_dir = ''
            
            # Remove namespaces for easier parsing
            smil_content = re.sub(r'xmlns="[^"]+"', '', smil_content)
            smil_content = re.sub(r'xmlns:[a-z]+="[^"]+"', '', smil_content)
            smil_content = re.sub(r'epub:', '', smil_content)
            
            root = ET.fromstring(smil_content)
            
            # Find all <par> elements (parallel containers with text + audio)
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
        
        Example:
            <par id="ch02-sentence21">
                <text src="../xhtml/ch02.html#ch02-sentence21"/>
                <audio src="../Audio/00001-00005.mp4" clipBegin="55.603s" clipEnd="58.857s"/>
            </par>
        """
        text_elem = par.find('text')
        audio_elem = par.find('audio')
        
        if text_elem is None or audio_elem is None:
            return None
        
        # Parse audio timestamps
        clip_begin = self._parse_timestamp(audio_elem.get('clipBegin', '0s'))
        clip_end = self._parse_timestamp(audio_elem.get('clipEnd', '0s'))
        
        # Apply audio offset
        start_time = clip_begin + audio_offset
        end_time = clip_end + audio_offset
        
        # Get text content
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
        """
        Parse SMIL timestamp string to seconds.
        
        Formats:
            - "55.603s" -> 55.603
            - "1:30:45.5" -> 5445.5
            - "90.5" -> 90.5
        """
        if not ts_str:
            return 0.0
        
        ts_str = ts_str.strip()
        
        # Remove 's' suffix if present
        if ts_str.endswith('s'):
            ts_str = ts_str[:-1]
        
        # Check for HH:MM:SS format
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
        """
        Get text content from XHTML file referenced by SMIL.
        
        text_src format: "../xhtml/ch02.html#ch02-sentence21"
        """
        if not text_src:
            return None
        
        # Split into file path and fragment
        if '#' in text_src:
            file_path, fragment_id = text_src.split('#', 1)
        else:
            file_path = text_src
            fragment_id = None
        
        # Resolve full path
        full_path = self._resolve_path(smil_dir, file_path)
        
        # Load and cache XHTML
        if full_path not in self._xhtml_cache:
            soup = self._load_xhtml(zf, full_path)
            if soup:
                self._xhtml_cache[full_path] = soup
            else:
                return None
        
        soup = self._xhtml_cache.get(full_path)
        if not soup:
            return None
        
        # Find element by ID
        if fragment_id:
            element = soup.find(id=fragment_id)
            if element:
                # Use separator to handle nested spans properly
                text = element.get_text(separator=' ', strip=True)
                # Clean up multiple spaces
                text = re.sub(r'\s+', ' ', text).strip()
                return text
        
        return None
    
    def _load_xhtml(self, zf: zipfile.ZipFile, path: str) -> Optional[BeautifulSoup]:
        """Load and parse an XHTML file from the EPUB."""
        # Try different path variations
        for path_variant in [path, path.lstrip('/'), path.replace('\\', '/')]:
            try:
                content = zf.read(path_variant).decode('utf-8')
                return BeautifulSoup(content, 'html.parser')
            except KeyError:
                continue
            except Exception as e:
                logger.debug(f"Error loading XHTML {path_variant}: {e}")
                continue
        
        logger.warning(f"Could not find XHTML file: {path}")
        return None


def extract_transcript_from_epub(epub_path: str, output_path: str = None,
                                  audio_offset: float = 0.0) -> Optional[str]:
    """
    Convenience function to extract transcript from EPUB and save to JSON.
    
    Args:
        epub_path: Path to the EPUB file
        output_path: Path for output JSON (default: {epub_stem}_transcript.json)
        audio_offset: Offset to add to timestamps
        
    Returns:
        Path to the saved transcript file, or None if extraction failed
    """
    extractor = SmilExtractor()
    
    if not extractor.has_media_overlays(epub_path):
        logger.info(f"EPUB does not have media overlays: {epub_path}")
        return None
    
    transcript = extractor.extract_transcript(epub_path, audio_offset)
    
    if not transcript:
        logger.error(f"Failed to extract transcript from: {epub_path}")
        return None
    
    # Determine output path
    if output_path is None:
        output_path = str(Path(epub_path).with_suffix('.transcript.json'))
    
    # Save transcript
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(transcript, f, ensure_ascii=False)
    
    logger.info(f"💾 Saved transcript ({len(transcript)} segments) to: {output_path}")
    return output_path


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO)
    
    if len(sys.argv) < 2:
        print("Usage: python smil_extractor.py <epub_file>")
        sys.exit(1)
    
    epub_path = sys.argv[1]
    output_path = extract_transcript_from_epub(epub_path)
    
    if output_path:
        print(f"\n✅ Success! Transcript saved to: {output_path}")
    else:
        print("\n❌ Failed to extract transcript")
        sys.exit(1)

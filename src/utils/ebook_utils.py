# [START FILE: abs-kosync-enhanced/ebook_utils.py]
"""
Ebook Utilities for abs-kosync-bridge

"""
from typing import Optional

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, Tag
from lxml import html
import hashlib
import logging
import os
import re
import glob
import rapidfuzz
import zipfile
import shutil
import tempfile
from pathlib import Path
from collections import OrderedDict
from src.sync_clients.sync_client_interface import LocatorResult

logger = logging.getLogger(__name__)

# Import epubcfi library for accurate CFI parsing
import epubcfi

class LRUCache:
    def __init__(self, capacity: int = 3):
        self.cache = OrderedDict()
        self.capacity = capacity

    def get(self, key):
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key, value):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        while len(self.cache) > self.capacity:
            self.cache.popitem(last=False)

    def clear(self):
        self.cache.clear()


class EbookParser:
    def __init__(self, books_dir, epub_cache_dir=None):
        self.books_dir = Path(books_dir)
        self.epub_cache_dir = Path(epub_cache_dir) if epub_cache_dir else Path("/data/epub_cache")

        cache_size = int(os.getenv("EBOOK_CACHE_SIZE", 3))
        self.cache = LRUCache(capacity=cache_size)
        self.fuzzy_threshold = int(os.getenv("FUZZY_MATCH_THRESHOLD", 80))
        self.hash_method = os.getenv("KOSYNC_HASH_METHOD", "content").lower()
        self.useXpathSegmentFallback = os.getenv("XPATH_FALLBACK_TO_PREVIOUS_SEGMENT", "false").lower() == "true"

        logger.info(f"✅ EbookParser initialized (cache={cache_size}, hash={self.hash_method}, xpath_fallback={self.useXpathSegmentFallback})")

    def resolve_book_path(self, filename):
        try:
            safe_name = glob.escape(filename)
            return next(self.books_dir.glob(f"**/{safe_name}"))
        except StopIteration:
            pass

        for f in self.books_dir.rglob("*"):
            if f.name == filename:
                return f

        if self.epub_cache_dir.exists():
            cached_path = self.epub_cache_dir / filename
            if cached_path.exists():
                return cached_path

        raise FileNotFoundError(f"Could not locate {filename}")

    def get_kosync_id(self, filepath):
        filepath = Path(filepath)
        if self.hash_method == "filename":
            return hashlib.md5(filepath.name.encode('utf-8')).hexdigest()
        
        md5 = hashlib.md5()
        try:
            file_size = os.path.getsize(filepath)
            with open(filepath, 'rb') as f:
                for i in range(-1, 11):
                    offset = 0 if i == -1 else 1024 * (4 ** i)
                    if offset >= file_size:
                        break
                    f.seek(offset)
                    chunk = f.read(1024)
                    if not chunk:
                        break
                    md5.update(chunk)
            return md5.hexdigest()
        except Exception as e:
            logger.error(f"❌ Error computing hash for {filepath}: {e}")
            return None

    def _compute_koreader_hash_from_bytes(self, content):
        md5 = hashlib.md5()
        try:
            file_size = len(content)
            for i in range(-1, 11):
                offset = 0 if i == -1 else 1024 * (4 ** i)
                if offset >= file_size: break

                chunk = content[offset:offset + 1024]
                if not chunk: break
                md5.update(chunk)
            return md5.hexdigest()
        except Exception as e:
            logger.error(f"❌ Error computing KOReader hash from bytes: {e}")
            return None

    def get_kosync_id_from_bytes(self, filename, content):
        if self.hash_method == "filename":
            return hashlib.md5(filename.encode('utf-8')).hexdigest()
        return self._compute_koreader_hash_from_bytes(content)

    def extract_cover(self, filepath, output_path):
        """
        Extract cover image from EPUB to output_path.
        Returns True if successful, False otherwise.
        """
        try:
            filepath = Path(filepath)
            # 1. Try to get cover from metadata using ebooklib
            try:
                book = epub.read_epub(str(filepath))
                # Check for cover item
                cover_item = None

                # Method A: get_item_with_id('cover') or similar
                # ebooklib doesn't have a standard 'get_cover' but often it's in the manifest

                # Method B: Iterate items
                for item in book.get_items():
                    if item.get_type() == ebooklib.ITEM_IMAGE:
                        # naive check: is it named "cover"?
                        if 'cover' in item.get_name().lower():
                            cover_item = item
                            break
                    if item.get_type() == ebooklib.ITEM_COVER:
                        cover_item = item
                        break

                if cover_item:
                    with open(output_path, 'wb') as f:
                        f.write(cover_item.get_content())
                    logger.debug(f"Extracted cover for {filepath.name}")
                    return True
            except Exception as e:
                logger.debug(f"ebooklib cover extraction failed for {filepath.name}: {e}")

            # 2. Fallback: ZipFile (if ebooklib fails or returns nothing)
            # (ebooklib is basically a zip wrapper anyway, but sometimes direct zip access is easier if we just want the file)
            # For now, let's stick to the attempt above. If valid EPUB, ebooklib should handle it.

            return False

        except Exception as e:
            logger.error(f"❌ Error extracting cover from '{filepath}': {e}")
            return False

    def extract_text_and_map(self, filepath, progress_callback=None):
        """
        Used for fuzzy matching and general content extraction.
        Uses BeautifulSoup.
        """
        filepath = Path(filepath)
        if not filepath.exists():
            filepath = self.resolve_book_path(filepath.name)
        str_path = str(filepath)

        cached = self.cache.get(str_path)
        if cached:
            if progress_callback: progress_callback(1.0)
            return cached['text'], cached['map']

        logger.info(f"Parsing EPUB: {filepath.name}")

        try:
            book = epub.read_epub(str_path)
            full_text_parts = []
            spine_map = []
            current_idx = 0

            total_spine = len(book.spine)

            for i, item_ref in enumerate(book.spine):
                if progress_callback:
                    progress_callback(i / total_spine)

                item = book.get_item_with_id(item_ref[0])
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    soup = BeautifulSoup(item.get_content(), 'html.parser')
                    text = soup.get_text(separator=' ', strip=True)

                    start = current_idx
                    length = len(text)
                    end = current_idx + length

                    spine_map.append({
                        "start": start,
                        "end": end,
                        "spine_index": i + 1,
                        "href": item.get_name(),
                        "content": item.get_content()
                    })

                    full_text_parts.append(text)
                    current_idx = end + 1

            combined_text = " ".join(full_text_parts)
            self.cache.put(str_path, {'text': combined_text, 'map': spine_map})
            return combined_text, spine_map

        except Exception as e:
            logger.error(f"❌ Failed to parse EPUB '{filepath}': {e}")
            return "", []

    def get_text_at_percentage(self, filename, percentage):
        """Get text snippet at a given percentage through the book."""
        try:
            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            if not full_text:
                return None

            target_pos = int(len(full_text) * percentage)
            # Grab a window of text around the calculated character position
            start = max(0, target_pos - 400)
            end = min(len(full_text), target_pos + 400)

            return full_text[start:end]
        except Exception as e:
            logger.error(f"❌ Error getting text at percentage: {e}")
            return None

    def get_character_delta(self, filename, percentage_prev, percentage_new):
        """Calculate character difference between two percentages."""
        try:
            book_path = self.resolve_book_path(filename)
            full_text, _ = self.extract_text_and_map(book_path)
            if not full_text:
                return None
            total_len = len(full_text)
            return abs(int(total_len * percentage_prev) - int(total_len * percentage_new))
        except Exception as e:
            logger.error(f"❌ Error calculating character delta: {e}")
            return None

    # =========================================================================
    # STORYTELLER / READIUM / GENERAL UTILS
    # Uses BeautifulSoup for broad compatibility
    # =========================================================================

    def resolve_locator_id(self, filename, href, fragment_id):
        """
        Returns a text snippet starting at the element identified by href + #fragment_id.
        Useful for syncing from Storyteller or any Readium-based reader that uses DOM IDs.
        """
        try:
            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            target_item = None
            for item in spine_map:
                if href in item['href'] or item['href'] in href:
                    target_item = item
                    break

            if not target_item: return None

            soup = BeautifulSoup(target_item['content'], 'html.parser')
            clean_id = fragment_id.lstrip('#')
            element = soup.find(id=clean_id)

            if not element: return None

            current_offset = 0
            found_offset = -1
            all_strings = soup.find_all(string=True)

            for s in all_strings:
                if s.parent == element or element in s.parents:
                    found_offset = current_offset
                    break
                text_len = len(s.strip())
                if text_len == 0:
                    continue
                current_offset += text_len

            if found_offset == -1:
                # Fallback
                elem_text = element.get_text(separator=' ', strip=True)
                chapter_text = soup.get_text(separator=' ', strip=True)
                found_offset = chapter_text.find(elem_text)

            if found_offset == -1: return None

            global_offset = target_item['start'] + found_offset
            start = max(0, global_offset)
            end = min(len(full_text), global_offset + 500)
            return full_text[start:end]

        except Exception as e:
            logger.error(f"❌ Error resolving locator ID '{fragment_id}' in '{filename}': {e}")
            return None

    def _generate_css_selector(self, target_tag):
        """Generate a Readium-compatible CSS selector."""
        if not target_tag: return ""
        segments = []
        curr = target_tag
        while curr and curr.name != '[document]':
            if not isinstance(curr, Tag):
                curr = curr.parent
                continue
            index = 1
            sibling = curr.previous_sibling
            while sibling:
                if isinstance(sibling, Tag):
                    index += 1
                sibling = sibling.previous_sibling
            segments.append(f"{curr.name}:nth-child({index})")
            curr = curr.parent
        return " > ".join(reversed(segments))

    def _generate_cfi(self, spine_index, html_content, local_target_index):
        """Generate an EPUB CFI for Booklore/Readium."""
        soup = BeautifulSoup(html_content, 'html.parser')
        current_char_count = 0
        target_tag = None

        elements = soup.find_all(string=True)
        for string in elements:
            text_len = len(string.strip())
            if text_len == 0: continue
            if current_char_count + text_len >= local_target_index:
                target_tag = string.parent
                break
            current_char_count += text_len
            if current_char_count < local_target_index:
                current_char_count += 1

        if not target_tag:
            spine_step = (spine_index + 1) * 2
            return f"epubcfi(/6/{spine_step}!/4/2/1:0)"

        path_segments = []
        curr = target_tag
        while curr and curr.name != '[document]':
            if curr.name == 'body':
                path_segments.append("4")
                break
            index = 1
            sibling = curr.previous_sibling
            while sibling:
                if isinstance(sibling, Tag):
                    index += 1
                sibling = sibling.previous_sibling
            path_segments.append(str(index * 2))
            curr = curr.parent

        spine_step = (spine_index + 1) * 2
        element_path = "/".join(reversed(path_segments))
        return f"epubcfi(/6/{spine_step}!/{element_path}:0)"

    def _generate_xpath_bs4(self, html_content, local_target_index):
        """
        Original BS4 XPath generator (kept for fuzzy matching references).
        Returns: (xpath_string, target_tag_object, is_anchored)
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        current_char_count = 0
        target_tag = None

        elements = soup.find_all(string=True)
        for string in elements:
            text_len = len(string.strip())
            if text_len == 0: continue
            if current_char_count + text_len >= local_target_index:
                target_tag = string.parent
                break
            current_char_count += text_len
            if current_char_count < local_target_index:
                current_char_count += 1

        if not target_tag: return "/body/div/p[1]", None, False

        path_segments = []
        curr = target_tag
        found_anchor = False

        while curr and curr.name != '[document]':
            if curr.name == 'body':
                path_segments.append("body")
                break
            if curr.has_attr('id') and curr['id']:
                path_segments.append(f"*[@id='{curr['id']}']")
                found_anchor = True
                break
            index = 1
            sibling = curr.previous_sibling
            while sibling:
                if isinstance(sibling, Tag) and sibling.name == curr.name:
                    index += 1
                sibling = sibling.previous_sibling
            path_segments.append(f"{curr.name}[{index}]")
            curr = curr.parent

        xpath = "//" + "/".join(reversed(path_segments)) if found_anchor else "/" + "/".join(reversed(path_segments))
        return xpath, target_tag, found_anchor

    def find_text_location(self, filename, search_phrase, hint_percentage=None) -> Optional[LocatorResult]:
        """
        Uses BS4 Engine. Good for fuzzy matching phrases from external apps.
        Returns: LocatorResult or None
        """
        try:
            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            if not full_text:
                return None
            total_len = len(full_text)

            # [NEW] 0. Global Uniqueness Check (The "Anchor" Logic)
            # Try to find a 10-word sequence that appears EXACTLY once in the book.
            # This prevents jumping to duplicate phrases (e.g., "Chapter 1" in the ToC vs the actual chapter).
            clean_search = " ".join(search_phrase.split())
            words = clean_search.split()
            
            match_index = -1
            
            if len(words) >= 10:
                N = 10
                # Scan through the search phrase to find a unique anchor
                for i in range(len(words) - N + 1):
                    candidate = " ".join(words[i:i+N])
                    
                    # Check if this phrase exists exactly ONCE in the text
                    if full_text.count(candidate) == 1:
                        found_idx = full_text.find(candidate)
                        if found_idx != -1:
                            match_index = found_idx
                            logger.info(f"⚓ Found unique text anchor: '{candidate[:30]}...' at index {match_index}")
                            break
            
            # [End of NEW logic] - Continue to existing fallbacks

            # 1. Exact match (if anchor logic didn't find anything)
            if match_index == -1:
                match_index = full_text.find(search_phrase)

            # 2. Normalized match
            if match_index == -1:
                norm_content = self._normalize(full_text)
                norm_search = self._normalize(search_phrase)
                norm_index = norm_content.find(norm_search)
                if norm_index != -1:
                    match_index = int((norm_index / len(norm_content)) * total_len)

            # 3. Fuzzy match
            if match_index == -1:
                cutoff = self.fuzzy_threshold
                if hint_percentage is not None:
                    w_start = int(max(0, hint_percentage - 0.10) * total_len)
                    w_end = int(min(1.0, hint_percentage + 0.10) * total_len)
                    alignment = rapidfuzz.fuzz.partial_ratio_alignment(
                        search_phrase, full_text[w_start:w_end], score_cutoff=cutoff
                    )
                    if alignment: match_index = w_start + alignment.dest_start

                if match_index == -1:
                    alignment = rapidfuzz.fuzz.partial_ratio_alignment(
                        search_phrase, full_text, score_cutoff=cutoff
                    )
                    if alignment: match_index = alignment.dest_start

            if match_index != -1:
                percentage = match_index / total_len
                for item in spine_map:
                    if item['start'] <= match_index < item['end']:
                        local_index = match_index - item['start']

                        # Use BS4 generator here for Rich Locators
                        xpath_str, target_tag, is_anchored = self._generate_xpath_bs4(item['content'], local_index)
                        css_selector = self._generate_css_selector(target_tag)
                        cfi = self._generate_cfi(item['spine_index'] - 1, item['content'], local_index)

                        # FIX: Handle double slashes gracefully
                        doc_frag_prefix = f"/body/DocFragment[{item['spine_index']}]"
                        if xpath_str.startswith('//'):
                            final_xpath = doc_frag_prefix + xpath_str[1:] # //id -> /DocFragment/id (or keep // if valid)
                        elif xpath_str.startswith('/'):
                            final_xpath = doc_frag_prefix + xpath_str
                        else:
                            final_xpath = f"{doc_frag_prefix}/{xpath_str}"
                        # Calculate chapter progress (critical for Storyteller)
                        chapter_len = len(item['content']) # Rough approximation using HTML length
                        if hasattr(item, 'get_content'): # double check if item object available or just dict
                             pass 
                        
                        # better: use start/end from map
                        spine_item_len = item['end'] - item['start']
                        chapter_progress = 0.0
                        if spine_item_len > 0:
                            chapter_progress = local_index / spine_item_len

                        perfect_ko = self.get_perfect_ko_xpath(filename, match_index)

                        return LocatorResult(
                            percentage=percentage,
                            xpath=final_xpath,
                            perfect_ko_xpath=perfect_ko,
                            match_index=match_index,
                            cfi=cfi,
                            href=item['href'],
                            fragment=None,
                            css_selector=css_selector,
                            chapter_progress=chapter_progress
                        )

            return None
        except Exception as e:
            logger.error(f"❌ Error finding text in '{filename}': {e}")
            return None

    def _normalize(self, text):
        return re.sub(r'[^a-z0-9]', '', text.lower())

    # =========================================================================
    # KOREADER PERFECT SYNC
    # Uses LXML and "Hybrid" Logic (ID + Text Offset)
    # UPDATED: STRICT WHITESPACE STRIPPING (Fixes "Behind by a page" / Undercounting)
    # =========================================================================

    def get_perfect_ko_xpath(self, filename, position=0) -> Optional[str]:
        """
        Generate KOReader XPath for a specific character position in the book.
        Simplified and focused approach that finds actual text elements.
        """
        try:
            # Get full text and spine mapping
            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            if not full_text or not spine_map:
                return None

            # Clamp position to valid range
            position = max(0, min(position, len(full_text) - 1))

            # Find which spine item contains this position
            target_item = next((item for item in spine_map
                              if item['start'] <= position < item['end']), spine_map[-1])

            local_pos = position - target_item['start']

            # Parse HTML content
            tree = html.fromstring(target_item['content'])

            # Find all elements that contain text, in document order
            text_elements = []
            current_count = 0
            SEPARATOR_LEN = 1

            for element in tree.iter():
                if element.text and element.text.strip():
                    text_len = len(element.text.strip())
                    text_elements.append({
                        'element': element,
                        'start_pos': current_count,
                        'end_pos': current_count + text_len + SEPARATOR_LEN,
                        'text_len': text_len
                    })
                    current_count += (text_len + SEPARATOR_LEN)

                if element.tail and element.tail.strip():
                    tail_len = len(element.tail.strip())
                    text_elements.append({
                        'element': element,
                        'start_pos': current_count,
                        'end_pos': current_count + tail_len + SEPARATOR_LEN,
                        'text_len': tail_len,
                        'is_tail': True
                    })
                    current_count += (tail_len + SEPARATOR_LEN)

            # Find the element that contains our target position
            target_element = None
            target_offset = 0
            is_tail = False
            target_text_len = 0

            for elem_info in text_elements:
                if elem_info['start_pos'] <= local_pos < elem_info['end_pos']:
                    target_element = elem_info['element']
                    target_offset = local_pos - elem_info['start_pos']
                    is_tail = elem_info.get('is_tail', False)
                    target_text_len = elem_info.get('text_len', 0)
                    break

            if target_element is None:
                # Fallback: use the first text-containing element
                if text_elements:
                    target_element = text_elements[0]['element']
                    target_offset = 0
                    is_tail = text_elements[0].get('is_tail', False)
                    target_text_len = text_elements[0].get('text_len', 0)
                else:
                    # Last resort: find any element with text
                    for elem in tree.xpath('.//p | .//span | .//em | .//strong | .//st | .//div'):
                        if elem.text and elem.text.strip():
                            target_element = elem
                            target_offset = 0
                            target_text_len = len(elem.text.strip())
                            break

            if target_element is None:
                logger.warning(f"⚠️ No text elements found in spine {target_item['spine_index']}")
                return None

            # Safety Check: Prevent Out-Of-Bounds offsets due to parser drift
            if target_text_len > 0 and target_offset > target_text_len + 1:
                logger.warning(f"⚠️ KOReader XPath Safety: Offset {target_offset} > text len {target_text_len} for '{target_element.tag}' — Rejecting to prevent crash")
                return None

            # Build xpath for the target element
            if is_tail:
                # Tail text belongs to the parent container, not the element itself
                parent = target_element.getparent()
                if parent is None:
                    # Should not happen for valid HTML body content
                    xpath = self._build_xpath(target_element)
                    return f"/body/DocFragment[{target_item['spine_index']}]/{xpath}/text().{target_offset}"

                xpath = self._build_xpath(parent)
                
                # Calculate which text node of the parent this is
                # XPath text() nodes are 1-based indices of text children
                text_node_index = 0
                if parent.text: text_node_index += 1
                
                for child in parent:
                    if child == target_element:
                        if child.tail: text_node_index += 1
                        break
                    if child.tail: text_node_index += 1
                
                # Should be at least 1 since we found it in text_elements check
                suffix = f"/text()[{text_node_index}]" if text_node_index > 0 else "/text()"
                return f"/body/DocFragment[{target_item['spine_index']}]/{xpath}{suffix}.{target_offset}"
            else:
                # Regular element text
                xpath = self._build_xpath(target_element)
                return f"/body/DocFragment[{target_item['spine_index']}]/{xpath}/text().{target_offset}"

        except Exception as e:
            logger.error(f"❌ Error generating KOReader XPath: {e}")
            return None

    def _has_text_content(self, element):
        """Check if element directly contains text (not just in children)."""
        return element.text and element.text.strip() and len(element.text.strip()) > 0

    def _build_xpath(self, element):
        """Build XPath for an element, ensuring proper KOReader format."""
        parts = []
        current = element

        while current is not None and current.tag not in ['html', 'document']:
            # Get siblings of same tag to determine index
            parent = current.getparent()
            if parent is not None:
                siblings = [s for s in parent if s.tag == current.tag]
                if len(siblings) > 1:
                    index = siblings.index(current) + 1
                    parts.insert(0, f"{current.tag}[{index}]")
                else:
                    parts.insert(0, current.tag)
            else:
                parts.insert(0, current.tag)
            current = parent

        # Clean up the path
        if parts and parts[0] == 'html':
            parts.pop(0)
        if not parts or parts[0] != 'body':
            parts.insert(0, 'body')

        # If we have no meaningful path, create a default
        if len(parts) <= 1:  # Just 'body' or empty
            parts = ['body', 'p[1]']

        return '/'.join(parts)

    def resolve_xpath(self, filename, xpath_str):
        """
        RESOLVER:
        Uses LXML to find the target element, then searches for its text in the
        BS4-generated full_text to ensure alignment (Fixes Parser Drift).
        """
        try:
            logger.debug(f"🔍 Resolving XPath (Hybrid): {xpath_str}")

            match = re.search(r'DocFragment\[(\d+)]', xpath_str)
            if not match:
                return None
            spine_index = int(match.group(1))

            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            target_item = next((i for i in spine_map if i['spine_index'] == spine_index), None)
            if not target_item:
                return None

            # Parse path and offset
            relative_path = xpath_str.split(f"DocFragment[{spine_index}]")[-1]
            offset_match = re.search(r'/text\(\)\.(\d+)$', relative_path)
            target_offset = int(offset_match.group(1)) if offset_match else 0
            clean_xpath = re.sub(r'/text\(\)\.(\d+)$', '', relative_path)

            if clean_xpath.startswith('/'):
                clean_xpath = '.' + clean_xpath

            tree = html.fromstring(target_item['content'])
            
            elements = []
            try:
                elements = tree.xpath(clean_xpath)
            except Exception as e:
                logger.debug(f"XPath query failed: {e}")
            
            # [Fallback logic from original code for finding elements...]
            if not elements and clean_xpath.startswith('./'):
                try: elements = tree.xpath(clean_xpath[2:])
                except Exception: pass

            if not elements:
                id_match = re.search(r"@id='([^']+)'", clean_xpath)
                if id_match:
                    try: elements = tree.xpath(f"//*[@id='{id_match.group(1)}']")
                    except Exception: pass

            if not elements:
                simple_path = re.sub(r'\[\d+]', '', clean_xpath)
                try: elements = tree.xpath(simple_path)
                except Exception: pass

            if not elements:
                logger.warning(f"⚠️ Could not resolve XPath in {filename}: {clean_xpath}")
                return None

            target_node = elements[0]

            # [NEW LOGIC STARTS HERE]
            # Instead of calculating offset via LXML iteration (which drifts),
            # grab the text and FIND it in the spine item content.
            
            # 1. Extract a unique-ish fingerprint from the node
            node_text = ""
            if target_node.text: node_text += target_node.text.strip()
            if target_node.tail: node_text += " " + target_node.tail.strip()
            
            # If node text is too short, grab parent context
            if len(node_text) < 20:
                parent = target_node.getparent()
                if parent is not None:
                    node_text = parent.text_content().strip()

            clean_anchor = " ".join(node_text.split())
            if not clean_anchor:
                return None

            # 2. Find this anchor in the BS4 content (spine_map item)
            # We search specifically in this chapter's content to minimize false positives
            bs4_chapter_text = BeautifulSoup(target_item['content'], 'html.parser').get_text(separator=' ', strip=True)
            
            local_start_index = bs4_chapter_text.find(clean_anchor)
            
            if local_start_index != -1:
                # Found it! Calculate global position
                # Add target_offset (clamped to length of anchor)
                safe_offset = min(target_offset, len(clean_anchor))
                global_index = target_item['start'] + local_start_index + safe_offset
                
                # 3. Return text from the Main Source of Truth (full_text)
                start = max(0, global_index)
                end = min(len(full_text), global_index + 600) # Grab enough context
                return full_text[start:end]
            
            else:
                # Fallback: If exact match fails (rare), try the old calculation method
                # (This preserves old behavior if the new matching fails)
                logger.debug("Exact text match failed, falling back to LXML offset calculation")
                # Falling back to strict calculation (Logic from original implementation)
                
                preceding_len = 0
                found_target = False
                SEPARATOR_LEN = 1

                for node in tree.iter():
                    if node == target_node:
                        found_target = True
                        if node.text and target_offset > 0:
                            raw_segment = node.text[:min(len(node.text), target_offset)]
                            preceding_len += len(raw_segment.strip())
                        elif target_offset > 0:
                            preceding_len += target_offset
                        break

                    if node.text and node.text.strip():
                        preceding_len += (len(node.text.strip()) + SEPARATOR_LEN)
                    if node.tail and node.tail.strip():
                        preceding_len += (len(node.tail.strip()) + SEPARATOR_LEN)
                
                if found_target:
                     local_pos = preceding_len
                     global_offset = target_item['start'] + local_pos
                     start = max(0, global_offset)
                     end = min(len(full_text), global_offset + 500)
                     return full_text[start:end]

                return None

        except Exception as e:
            logger.error(f"❌ Error resolving XPath '{xpath_str}': {e}")
            return None

    def get_text_around_cfi(self, filename, cfi, context=50):
        """
        Returns a text fragment of length 2*context centered on the position indicated by the CFI.
        Uses the epubcfi library for precise parsing.

        Example supported CFI: epubcfi(/6/16[chapter_6]!/4/2[book-columns]/2[book-inner]/268/4/2[kobo.134.3]/1:11)
        """
        try:
            # Parse CFI using the epubcfi library
            parsed_cfi = epubcfi.parse(cfi)

            # Extract spine information and element steps
            spine_step = None
            element_steps = []

            for step in parsed_cfi.steps:
                if hasattr(step, 'index'):
                    if step.index == 6:  # Skip spine reference marker
                        continue
                    elif not spine_step and step.index > 6:  # First step after /6/ is spine
                        spine_step = step.index
                    elif isinstance(step, epubcfi.cfi.Step):
                        element_steps.append(step)
                # Skip Redirect objects (!)

            char_offset = parsed_cfi.offset.value if parsed_cfi.offset else 0

            if not spine_step:
                logger.error(f"❌ Could not extract spine step from CFI: '{cfi}'")
                return None

            # Load the EPUB and find the spine item
            book_path = self.resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            # Calculate spine index (CFI spine steps are 2x the actual index)
            spine_index = (spine_step // 2) - 1
            if not (0 <= spine_index < len(spine_map)):
                logger.error(f"❌ Spine index {spine_index} out of range for CFI '{cfi}'")
                return None

            item = spine_map[spine_index]

            # Parse the HTML content with lxml for precise navigation
            tree = html.fromstring(item['content'])

            # Follow the CFI path precisely through the DOM
            current_element = tree
            text_count = 0

            logger.debug(f"Following CFI path with {len(element_steps)} steps")

            for i, step in enumerate(element_steps):
                if not hasattr(step, 'index'):
                    continue

                step_index = step.index
                step_assertion = step.assertion

                logger.debug(f"Step {i}: index={step_index}, assertion={step_assertion}")

                if step_assertion:
                    # Look for element with specific ID or class
                    candidates = current_element.xpath(f".//*[contains(@id, '{step_assertion}') or contains(@class, '{step_assertion}')]")
                    if candidates:
                        current_element = candidates[0]
                        logger.debug(f"Found element with assertion: {step_assertion}")
                        continue

                # CFI uses 1-based indexing, even numbers for elements
                if step_index % 2 == 0:  # Even number = element
                    element_index = (step_index // 2) - 1
                    children = [child for child in current_element if hasattr(child, 'tag')]

                    if 0 <= element_index < len(children):
                        current_element = children[element_index]
                        logger.debug(f"Navigated to child element {element_index}: {current_element.tag}")
                    else:
                        logger.warning(f"⚠️ Element index {element_index} out of range (have {len(children)} children)")
                        break
                else:  # Odd number = text node
                    text_index = (step_index // 2)
                    # For text nodes, we need to count text content
                    text_nodes = []
                    for child in current_element:
                        if child.text and child.text.strip():
                            text_nodes.append(child.text.strip())
                        if child.tail and child.tail.strip():
                            text_nodes.append(child.tail.strip())

                    if 0 <= text_index < len(text_nodes):
                        # Calculate position up to this text node
                        text_count += sum(len(text) for text in text_nodes[:text_index])
                        logger.debug(f"Text node {text_index}, accumulated count: {text_count}")
                    break

            # Calculate text position within the current element
            if current_element is not None:
                # Get all text content up to the current element's position in the document
                soup = BeautifulSoup(item['content'], 'html.parser')
                chapter_text = soup.get_text(separator=' ', strip=True)

                # Find the current element's text in the chapter
                element_text = ""
                if hasattr(current_element, 'text_content'):
                    element_text = current_element.text_content()

                if element_text and len(element_text.strip()) > 5:
                    # Find where this element's content appears in the chapter
                    element_start = chapter_text.find(element_text.strip()[:50])
                    if element_start != -1:
                        local_offset = element_start + char_offset
                    else:
                        # Fallback: use text_count + char_offset
                        local_offset = text_count + char_offset
                else:
                    local_offset = text_count + char_offset
            else:
                local_offset = text_count + char_offset

            # Clamp to chapter bounds
            chapter_text = BeautifulSoup(item['content'], 'html.parser').get_text(separator=' ', strip=True)
            local_offset = min(max(0, local_offset), len(chapter_text))

            # Calculate global position
            global_offset = item['start'] + local_offset

            # Extract context
            start_pos = max(0, global_offset - context)
            end_pos = min(len(full_text), global_offset + context)

            snippet = full_text[start_pos:end_pos]
            logger.info(f"Snippet extracted: {snippet[:30]}...")
            return snippet

        except Exception as e:
            logger.error(f"❌ Error using epubcfi library for '{cfi}': {e}")
            return None


def sanitize_storyteller_artifacts(epub_path: Path) -> bool:
    """
    Sanitize Storyteller EPUBs by removing specific <span> tags that break alignment.
    Removes <span id="par..."> and <span id="sent..."> tags while preserving content.
    """
    try:
        epub_path = Path(epub_path)
        logger.info(f"Sanitizing Storyteller artifacts in: {epub_path.name}")
        
        # Create temp dir
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # Extract EPUB
            with zipfile.ZipFile(epub_path, 'r') as zip_ref:
                zip_ref.extractall(temp_path)
                
            # Iterate through HTML/XHTML files
            modified_count = 0
            for root, dirs, files in os.walk(temp_path):
                for file in files:
                    if file.endswith(('.html', '.xhtml', '.htm')):
                        file_path = Path(root) / file
                        
                        try:
                            # Read with utf-8
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                
                            soup = BeautifulSoup(content, 'html.parser')
                            file_modified = False
                            
                            # Find spans with ids starting with 'par' or 'sent'
                            # Storyteller uses id="par0", id="sent0", etc. which break our specific alignment engines
                            for span in soup.find_all('span', id=re.compile(r'^(par|sent)\d+')):
                                span.unwrap() # Remove the tag but keep contents
                                file_modified = True
                                modified_count += 1
                                
                            if file_modified:
                                with open(file_path, 'w', encoding='utf-8') as f:
                                    f.write(str(soup))
                                    
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to sanitize file '{file}': {e}")
                            
            if modified_count > 0:
                logger.info(f"Removed {modified_count} Storyteller tags. Repacking...")
                
                # Create a new zip file
                temp_epub = temp_path / "sanitized.epub"
                with zipfile.ZipFile(temp_epub, 'w', zipfile.ZIP_DEFLATED) as zip_out:
                    for root, dirs, files in os.walk(temp_path):
                        for file in files:
                            file_path = Path(root) / file
                            if file == "sanitized.epub": continue
                            
                            # Archive name should be relative to temp_path
                            arcname = file_path.relative_to(temp_path)
                            zip_out.write(file_path, arcname)
                            
                # Replace original
                # Force move (replace)
                shutil.move(str(temp_epub), str(epub_path))
                logger.info(f"✅ Successfully sanitized: {epub_path.name}")
                return True
            else:
                logger.debug(f"No Storyteller tags found in {epub_path.name}. Skipping repack.")
                return True # It's valid, just didn't need changes
            
    except Exception as e:
        logger.error(f"❌ Error sanitizing EPUB {epub_path}: {e}")
        return False
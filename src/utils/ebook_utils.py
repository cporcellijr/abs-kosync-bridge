# [START FILE: abs-kosync-enhanced/ebook_utils.py]
"""
Ebook Utilities for abs-kosync-bridge

ULTIMATE HYBRID VERSION:
- [Engine A] BeautifulSoup: For fuzzy matching, Storyteller/Readium support, and rich locators.
- [Engine B] LXML: For "Perfect" KOReader sync (precise /text().OFFSET handling).
- Robustness: Prefers ID anchors when available, falls back to positional indexing.
- Complete Storyteller/Readium locator support with href + #id resolution.
"""
from typing import Optional

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, Tag
from lxml import html  # Engine B: LXML for precise sync
import hashlib
import logging
import os
import re
import glob
import rapidfuzz
from pathlib import Path
from collections import OrderedDict
from src.sync_clients.sync_client_interface import LocatorResult

logger = logging.getLogger(__name__)


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

        logger.info(f"EbookParser initialized (cache={cache_size}, hash={self.hash_method}, xpath_fallback={self.useXpathSegmentFallback})")

    def _resolve_book_path(self, filename):
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
        try:
            with open(filepath, 'rb') as f:
                return hashlib.md5(f.read(4096)).hexdigest()
        except Exception as e:
            logger.error(f"Error computing hash for {filepath}: {e}")
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
            logger.error(f"Error computing KOReader hash from bytes: {e}")
            return None

    def get_kosync_id_from_bytes(self, filename, content):
        if self.hash_method == "filename":
            return hashlib.md5(filename.encode('utf-8')).hexdigest()
        return self._compute_koreader_hash_from_bytes(content)

    def extract_text_and_map(self, filepath):
        """
        Used for fuzzy matching and general content extraction.
        Uses BeautifulSoup (Engine A).
        """
        filepath = Path(filepath)
        if not filepath.exists():
            filepath = self._resolve_book_path(filepath.name)
        str_path = str(filepath)

        cached = self.cache.get(str_path)
        if cached:
            return cached['text'], cached['map']

        logger.info(f"Parsing EPUB: {filepath.name}")

        try:
            book = epub.read_epub(str_path)
            full_text_parts = []
            spine_map = []
            current_idx = 0

            for i, item_ref in enumerate(book.spine):
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
            logger.error(f"Failed to parse EPUB {filepath}: {e}")
            return "", []

    def get_text_at_percentage(self, filename, percentage):
        """Get text snippet at a given percentage through the book."""
        try:
            book_path = self._resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            if not full_text:
                return None

            target_pos = int(len(full_text) * percentage)
            # Grab a window of text around the calculated character position
            start = max(0, target_pos - 400)
            end = min(len(full_text), target_pos + 400)

            return full_text[start:end]
        except Exception as e:
            logger.error(f"Error getting text at percentage: {e}")
            return None

    def get_character_delta(self, filename, percentage_prev, percentage_new):
        """Calculate character difference between two percentages."""
        try:
            book_path = self._resolve_book_path(filename)
            full_text, _ = self.extract_text_and_map(book_path)
            if not full_text:
                return None
            total_len = len(full_text)
            return abs(int(total_len * percentage_prev) - int(total_len * percentage_new))
        except Exception as e:
            logger.error(f"Error calculating character delta: {e}")
            return None

    # =========================================================================
    # [ENGINE A] STORYTELLER / READIUM / GENERAL UTILS
    # Uses BeautifulSoup for broad compatibility
    # =========================================================================

    def resolve_locator_id(self, filename, href, fragment_id):
        """
        Returns a text snippet starting at the element identified by href + #fragment_id.
        Useful for syncing from Storyteller or any Readium-based reader that uses DOM IDs.
        """
        try:
            book_path = self._resolve_book_path(filename)
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
            logger.error(f"Error resolving locator ID {fragment_id} in {filename}: {e}")
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
            book_path = self._resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            if not full_text:
                return None
            total_len = len(full_text)

            # 1. Exact match
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
                        return LocatorResult(
                            percentage=percentage,
                            xpath=final_xpath,
                            match_index=match_index,
                            cfi=cfi,
                            href=item['href'],
                            fragment=None,
                            css_selector=css_selector
                        )

            return None
        except Exception as e:
            logger.error(f"Error finding text in {filename}: {e}")
            return None

    def _normalize(self, text):
        return re.sub(r'[^a-z0-9]', '', text.lower())

    # =========================================================================
    # [ENGINE B] KOREADER PERFECT SYNC
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
            book_path = self._resolve_book_path(filename)
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

            for element in tree.iter():
                if element.text and element.text.strip():
                    text_len = len(element.text.strip())
                    text_elements.append({
                        'element': element,
                        'start_pos': current_count,
                        'end_pos': current_count + text_len,
                        'text_len': text_len
                    })
                    current_count += text_len

                if element.tail and element.tail.strip():
                    tail_len = len(element.tail.strip())
                    text_elements.append({
                        'element': element,
                        'start_pos': current_count,
                        'end_pos': current_count + tail_len,
                        'text_len': tail_len,
                        'is_tail': True
                    })
                    current_count += tail_len

            # Find the element that contains our target position
            target_element = None
            target_offset = 0

            for elem_info in text_elements:
                if elem_info['start_pos'] <= local_pos < elem_info['end_pos']:
                    target_element = elem_info['element']
                    target_offset = local_pos - elem_info['start_pos']
                    break

            if target_element is None:
                # Fallback: use the first text-containing element
                if text_elements:
                    target_element = text_elements[0]['element']
                    target_offset = 0
                else:
                    # Last resort: find any element with text
                    for elem in tree.xpath('.//p | .//span | .//em | .//strong | .//st | .//div'):
                        if elem.text and elem.text.strip():
                            target_element = elem
                            target_offset = 0
                            break

            if target_element is None:
                logger.warning(f"No text elements found in spine {target_item['spine_index']}")
                return None

            # Build xpath for the target element
            xpath = self._build_xpath(target_element)
            return f"/body/DocFragment[{target_item['spine_index']}]/{xpath}/text().{target_offset}"

        except Exception as e:
            logger.error(f"Error generating KOReader XPath: {e}")
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
        HYBRID RESOLVER:
        Uses LXML to handle KOReader's /text().123 format accurately.
        Includes WHITESPACE STRIPPING to align with get_xpath_and_percentage.
        """
        try:
            logger.debug(f"🔍 Resolving XPath (Hybrid): {xpath_str}")

            match = re.search(r'DocFragment\[(\d+)]', xpath_str)
            if not match:
                return None
            spine_index = int(match.group(1))

            book_path = self._resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            target_item = next((i for i in spine_map if i['spine_index'] == spine_index), None)
            if not target_item:
                return None

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

            if not elements and clean_xpath.startswith('./'):
                try: elements = tree.xpath(clean_xpath[2:])
                except: pass

            if not elements:
                id_match = re.search(r"@id='([^']+)'", clean_xpath)
                if id_match:
                    try: elements = tree.xpath(f"//*[@id='{id_match.group(1)}']")
                    except: pass

            if not elements:
                simple_path = re.sub(r'\[\d+]', '', clean_xpath)
                try: elements = tree.xpath(simple_path)
                except: pass

            if not elements:
                logger.warning(f"❌ Could not resolve XPath in {filename}: {clean_xpath}")
                return None

            target_node = elements[0]

            # Calculate position by counting CLEAN text length
            preceding_len = 0
            found_target = False

            for node in tree.iter():
                if node == target_node:
                    found_target = True
                    # The offset from KOReader is a RAW offset into node.text (or tail)
                    # We need to convert this RAW offset into CLEAN length contribution.
                    if node.text and target_offset > 0:
                        # Safety check for slice range
                        raw_segment = node.text[:min(len(node.text), target_offset)]
                        preceding_len += len(raw_segment.strip())
                    elif target_offset > 0:
                        # Fallback for tail targeting or weird state
                        preceding_len += target_offset # Best guess
                    break

                if node.text:
                    preceding_len += len(node.text.strip())
                if node.tail:
                    preceding_len += len(node.tail.strip())

            if not found_target:
                logger.warning(f"❌ Target node not found in iteration")
                return None

            local_pos = preceding_len
            global_offset = target_item['start'] + local_pos

            start = max(0, global_offset)
            end = min(len(full_text), global_offset + 500)
            return full_text[start:end]

        except Exception as e:
            logger.error(f"Error resolving XPath {xpath_str}: {e}")
            return None

    def get_text_around_cfi(self, filename, cfi, context=50):
        """
        Returns a text fragment of length 2*context centered on the position indicated by the CFI.
        Supports real-world CFIs with bracketed IDs and complex paths.
        If the CFI cannot be resolved, returns None.
        """
        try:
            import re
            book_path = self._resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            # Robust CFI parsing: extract first number after /6/ and number after colon
            # Example: epubcfi(/6/10[Afscheid_voor_even-2]!/4[Afscheid_voor_even-2]/.../1:29)
            cfi_pattern = r"epubcfi\(/6/(\d+)(?:\[[^\]]*\])?!.*:(\d+)\)"
            match = re.match(cfi_pattern, cfi)
            if not match:
                logger.error(f"Invalid or unsupported CFI format: {cfi}")
                return None
            spine_step = int(match.group(1))
            char_offset = int(match.group(2))

            # EPUB CFI spine_step is 2x the spine index (see _generate_cfi)
            spine_index = (spine_step // 2) - 1
            if not (0 <= spine_index < len(spine_map)):
                logger.error(f"Spine index {spine_index} out of range for CFI {cfi}")
                return None
            item = spine_map[spine_index]
            start = item['start'] + char_offset
            end = start + context
            begin = max(0, start - context)
            snippet = full_text[begin:end]
            return snippet
        except Exception as e:
            logger.error(f"Error resolving CFI {cfi} in {filename}: {e}")
            return None
# [END FILE]

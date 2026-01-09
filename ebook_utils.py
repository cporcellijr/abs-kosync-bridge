# [START FILE: abs-kosync-enhanced/ebook_utils.py]
"""
Ebook Utilities for abs-kosync-bridge

FINAL HYBRID MERGE VERSION:
- [Engine A] BeautifulSoup: For fuzzy matching, Storyteller/Readium support, and rich locators.
- [Engine B] LXML: For "Perfect" KOReader sync (precise /text().OFFSET handling).
- Robustness: Prefers ID anchors when available, falls back to positional indexing.
"""

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

    # =========================================================================
    # [ENGINE A] STORYTELLER / READIUM / GENERAL UTILS
    # Uses BeautifulSoup for broad compatibility
    # =========================================================================

    def resolve_locator_id(self, filename, href, fragment_id):
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
                current_offset += len(s)

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
        """Original BS4 XPath generator (kept for fuzzy matching references)."""
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

    def find_text_location(self, filename, search_phrase, hint_percentage=None):
        """
        Uses BS4 Engine. Good for fuzzy matching phrases from external apps.
        """
        try:
            book_path = self._resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            if not full_text: return None, None
            total_len = len(full_text)
            match_index = -1

            match_index = full_text.find(search_phrase)
            if match_index == -1:
                norm_content = self._normalize(full_text)
                norm_search = self._normalize(search_phrase)
                match_index = norm_content.find(norm_search)
                if match_index != -1:
                    match_index = int((match_index / len(norm_content)) * total_len)

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

                        rich_locator = {
                            "href": item['href'],
                            "cssSelector": css_selector,
                            "xpath": final_xpath,
                            "cfi": cfi,
                            "match_index": match_index
                        }
                        return percentage, rich_locator

            return None, None
        except Exception as e:
            logger.error(f"Error finding text in {filename}: {e}")
            return None, None

    def _normalize(self, text):
        return re.sub(r'[^a-z0-9]', '', text.lower())

    # =========================================================================
    # [ENGINE B] KOREADER PERFECT SYNC
    # Uses LXML and "Hybrid" Logic (ID + Text Offset)
    # =========================================================================

    def get_xpath_and_percentage(self, filename, position=0):
    try:
        book_path = self._resolve_book_path(filename)
        full_text, spine_map = self.extract_text_and_map(book_path)
        total_length = len(full_text)
        
        if total_length == 0:
            return None, 0.0

        current_pos = min(max(0, position), total_length)
        percentage = current_pos / total_length

        # Find which spine item this falls into
        target_item = None
        local_pos = 0
        
        for item in spine_map:
            if item['start'] <= current_pos < item['end']:
                target_item = item
                local_pos = current_pos - item['start']
                break
        
        if not target_item:
            if spine_map: 
                target_item = spine_map[-1]
                local_pos = len(target_item['content'])
            else:
                return None, 1.0

        # Use LXML to generate the hybrid XPath
        tree = html.fromstring(target_item['content'])
        
        # Find the specific node at 'local_pos'
        current_count = 0
        target_node = None
        target_offset = 0

        for node in tree.iter():
            if node.text:
                node_len = len(node.text)
                if current_count + node_len >= local_pos:
                    target_node = node
                    target_offset = local_pos - current_count
                    break
                current_count += node_len
            if node.tail:
                tail_len = len(node.tail)
                if current_count + tail_len >= local_pos:
                    target_node = node  # ✅ FIXED: Was node.getparent()
                    target_offset = local_pos - current_count
                    break
                current_count += tail_len

        if target_node is None:
            raw_xpath = "/body/html"
            target_offset = 0
        else:
            raw_xpath = self._generate_hybrid_xpath_lxml(target_node)

        # ✅ FIXED: Ensure proper path joining
        if not raw_xpath.startswith('/'):
            raw_xpath = '/' + raw_xpath

        # KOReader format: /body/DocFragment[X]/.../text().Y
        doc_frag = f"/body/DocFragment[{target_item['spine_index']}]"
        final_xpath = f"{doc_frag}{raw_xpath}/text().{target_offset}"
        
        return final_xpath, percentage

    except Exception as e:
        logger.error(f"Error in get_xpath_and_percentage: {e}")
        return None, 0.0

    def _generate_hybrid_xpath_lxml(self, node):
        """
        Internal LXML helper: STRICT POSITIONAL ONLY.
        1. Removes ID anchoring (KOReader hates it).
        2. Removes 'html' root tag (KOReader expects path to start at body).
        """
        path = []
        current = node
        while current is not None:
            parent = current.getparent()
            if parent is None:
                path.insert(0, current.tag)
                break
                
            siblings = list(parent)
            # Count matching tags before this one
            matching_siblings = [s for s in siblings if s.tag == current.tag]
            
            if len(matching_siblings) > 1:
                index = matching_siblings.index(current) + 1
                path.insert(0, f"{current.tag}[{index}]")
            else:
                path.insert(0, current.tag)
            
            current = parent

        # FIX: KOReader expects paths like /body/DocFragment[X]/body/...
        # lxml generates /html/body/..., so we must remove the leading 'html'.
        if path and path[0] == 'html':
            path.pop(0)

        return "/".join(path)

    def resolve_xpath(self, filename, xpath_str):
    """
    HYBRID RESOLVER:
    Uses LXML to handle KOReader's /text().123 format accurately.
    """
    try:
        logger.debug(f"🔍 Resolving XPath (Hybrid): {xpath_str}")
        
        # Extract DocFragment index
        match = re.search(r'DocFragment\[(\d+)\]', xpath_str)
        if not match:
            return None
        spine_index = int(match.group(1))

        book_path = self._resolve_book_path(filename)
        full_text, spine_map = self.extract_text_and_map(book_path)
        
        target_item = next((i for i in spine_map if i['spine_index'] == spine_index), None)
        if not target_item:
            return None

        # Clean path and extract offset
        relative_path = xpath_str.split(f"DocFragment[{spine_index}]")[-1]
        
        # Check for offset suffix
        offset_match = re.search(r'/text\(\)\.(\d+)$', relative_path)
        target_offset = int(offset_match.group(1)) if offset_match else 0
        clean_xpath = re.sub(r'/text\(\)\.(\d+)$', '', relative_path)
        
        # ✅ FIX: Handle leading slash and make xpath work with lxml
        if clean_xpath.startswith('/'):
            clean_xpath = '.' + clean_xpath  # Make relative
        
        # LXML Parsing
        tree = html.fromstring(target_item['content'])
        
        # Attempt 1: Direct Lookup
        try:
            elements = tree.xpath(clean_xpath)
        except Exception as e:
            logger.debug(f"XPath query failed: {e}")
            elements = []
        
        # Attempt 2: Fallback - try without leading ./
        if not elements and clean_xpath.startswith('./'):
            try:
                elements = tree.xpath(clean_xpath[2:])
            except:
                pass
        
        # Attempt 3: Fallback (ID check)
        if not elements:
            logger.debug("⚠️ Direct XPath failed, trying fallback ID search")
            id_match = re.search(r"@id='([^']+)'", clean_xpath)
            if id_match:
                try:
                    elements = tree.xpath(f"//*[@id='{id_match.group(1)}']")
                except:
                    pass
        
        # Attempt 4: Just find by tag path (strip indices)
        if not elements:
            simple_path = re.sub(r'\[\d+\]', '', clean_xpath)
            try:
                elements = tree.xpath(simple_path)
            except:
                pass
        
        if not elements:
            logger.warning(f"❌ Could not resolve XPath in {filename}: {clean_xpath}")
            return None
        
        target_node = elements[0]
        
        # ✅ FIX: Calculate position by counting all text up to and including target node
        preceding_len = 0
        found_target = False
        
        for node in tree.iter():
            if node == target_node:
                found_target = True
                # Add the offset within this node
                # Note: offset could be in .text, we add it directly
                preceding_len += target_offset
                break
            if node.text:
                preceding_len += len(node.text)
            if node.tail:
                preceding_len += len(node.tail)
        
        if not found_target:
            logger.warning(f"❌ Target node not found in iteration")
            return None
        
        local_pos = preceding_len
        
        # Global offset
        global_offset = target_item['start'] + local_pos
        
        # Return text snippet
        start = max(0, global_offset)
        end = min(len(full_text), global_offset + 500)
        return full_text[start:end]

    except Exception as e:
        logger.error(f"Error resolving XPath {xpath_str}: {e}")
        return None
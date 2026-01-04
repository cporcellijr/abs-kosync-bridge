# [START FILE: abs-kosync-enhanced/ebook_utils.py]
"""
Ebook Utilities for abs-kosync-bridge

HARDENED VERSION with:
- LRU Cache (capacity=3) to prevent OOM
- Robust path resolution
- Rich Locator Support (href + cssSelector) for Storyteller
- Fixed Tuple Return signature in _generate_xpath
"""

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString, Tag
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
        if key not in self.cache: return None
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key, value):
        if key in self.cache: self.cache.move_to_end(key)
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
        logger.info(f"EbookParser initialized (cache={cache_size}, hash={self.hash_method})")

    def _resolve_book_path(self, filename):
        # First, search in books_dir (filesystem mount)
        try:
            safe_name = glob.escape(filename)
            return next(self.books_dir.glob(f"**/{safe_name}"))
        except StopIteration:
            pass
        for f in self.books_dir.rglob("*"):
            if f.name == filename: return f

        # Then, check epub_cache (downloaded from Booklore)
        if self.epub_cache_dir.exists():
            cached_path = self.epub_cache_dir / filename
            if cached_path.exists():
                return cached_path

        raise FileNotFoundError(f"Could not locate {filename}")

    def get_kosync_id(self, filepath):
        filepath = Path(filepath)
        if self.hash_method == "filename":
            return self._compute_filename_hash(filepath)
        return self._compute_koreader_hash(filepath)

    def _compute_filename_hash(self, filepath):
        return hashlib.md5(filepath.name.encode('utf-8')).hexdigest()

    def _compute_koreader_hash(self, filepath):
        md5 = hashlib.md5()
        try:
            file_size = os.path.getsize(filepath)
            with open(filepath, 'rb') as f:
                for i in range(-1, 11):
                    offset = 0 if i == -1 else 1024 * (4 ** i)
                    if offset >= file_size: break
                    f.seek(offset)
                    chunk = f.read(1024)
                    if not chunk: break
                    md5.update(chunk)
            return md5.hexdigest()
        except Exception as e:
            logger.error(f"Error computing KOReader hash: {e}")
            return None

    def extract_text_and_map(self, filepath):
        filepath = Path(filepath)
        if not filepath.exists():
            filepath = self._resolve_book_path(filepath.name)
        str_path = str(filepath)
        
        cached = self.cache.get(str_path)
        if cached: return cached['text'], cached['map']
        
        logger.info(f"Parsing EPUB: {filepath.name}")
        
        try:
            book = epub.read_epub(str(filepath))
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
                    
                    # Capture 'href' (internal filename) for Storyteller
                    item_href = item.get_name()

                    spine_map.append({
                        "start": start,
                        "end": end,
                        "spine_index": i + 1,
                        "href": item_href,
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
        """
        Generate an EPUB CFI (Canonical Fragment Identifier) for Booklore.
        CFI format: epubcfi(/6/{spine_step}!/4/{element_path})
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        current_char_count = 0
        target_tag = None
        char_offset = 0

        elements = soup.find_all(string=True)
        for string in elements:
            text_len = len(string.strip())
            if text_len == 0: continue
            if current_char_count + text_len >= local_target_index:
                target_tag = string.parent
                char_offset = local_target_index - current_char_count
                break
            current_char_count += text_len
            if current_char_count < local_target_index: current_char_count += 1

        if not target_tag:
            # Fallback CFI pointing to start of spine item
            spine_step = (spine_index + 1) * 2
            return f"epubcfi(/6/{spine_step}!/4/2/1:0)"

        # Build CFI path from target element to body
        path_segments = []
        curr = target_tag
        while curr and curr.name != '[document]':
            if curr.name == 'body':
                path_segments.append("4")  # body is always /4 in CFI
                break
            # Count element position among same-type siblings
            index = 1
            sibling = curr.previous_sibling
            while sibling:
                if isinstance(sibling, Tag):
                    index += 1
                sibling = sibling.previous_sibling
            # CFI uses even numbers for elements (index * 2)
            path_segments.append(str(index * 2))
            curr = curr.parent

        # Spine step in CFI (spine_index * 2 + 2 for 1-based, even numbers)
        spine_step = (spine_index + 1) * 2

        # Build the CFI string
        element_path = "/".join(reversed(path_segments))
        cfi = f"epubcfi(/6/{spine_step}!/{element_path}:0)"

        return cfi

    def _generate_xpath(self, html_content, local_target_index):
        """
        Generate XPath and return the DOM Tag for CSS generation.
        Returns: (xpath_string, target_tag_object)
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
            if current_char_count < local_target_index: current_char_count += 1
        
        # FIX: Must return tuple (str, None) to match signature
        if not target_tag: return "/body/div/p[1]", None
        
        path_segments = []
        curr = target_tag
        while curr and curr.name != '[document]':
            if curr.name == 'body':
                path_segments.append("body")
                break
            index = 1
            sibling = curr.previous_sibling
            while sibling:
                if isinstance(sibling, Tag) and sibling.name == curr.name:
                    index += 1
                sibling = sibling.previous_sibling
            path_segments.append(f"{curr.name}[{index}]")
            curr = curr.parent
        
        xpath = "/" + "/".join(reversed(path_segments))
        return xpath, target_tag

    def _normalize(self, text):
        return re.sub(r'[^a-z0-9]', '', text.lower())

    def find_text_location(self, filename, search_phrase, hint_percentage=None):
        """
        Find text location.
        Returns: (percentage, rich_locator_dict) or (None, None)
        rich_locator_dict contains: href, cssSelector, xpath, match_index
        """
        try:
            book_path = self._resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)
            
            if not full_text: return None, None
            
            total_len = len(full_text)
            match_index = -1
            
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

                        # Generate XPath (for KoReader), CSS Selector (for Storyteller), and CFI (for Booklore)
                        xpath_str, target_tag = self._generate_xpath(item['content'], local_index)
                        css_selector = self._generate_css_selector(target_tag)
                        cfi = self._generate_cfi(item['spine_index'] - 1, item['content'], local_index)

                        rich_locator = {
                            "href": item['href'],
                            "cssSelector": css_selector,
                            "xpath": f"/body/DocFragment[{item['spine_index']}]{xpath_str}",
                            "cfi": cfi,
                            "match_index": match_index
                        }

                        return percentage, rich_locator
            
            return None, None
            
        except Exception as e:
            logger.error(f"Error finding text in {filename}: {e}")
            return None, None

    def get_text_at_percentage(self, filename, percentage):
        try:
            book_path = self._resolve_book_path(filename)
            full_text, _ = self.extract_text_and_map(book_path)
            if not full_text: return None
            
            target_index = int(len(full_text) * percentage)
            start = max(0, target_index - 450)
            end = min(len(full_text), target_index + 450)
            return full_text[start:end]
        except Exception: return None

    def get_character_delta(self, filename, percentage_prev, percentage_new):
        try:
            book_path = self._resolve_book_path(filename)
            full_text, _ = self.extract_text_and_map(book_path)
            if not full_text: return None
            total_len = len(full_text)
            return abs(int(total_len * percentage_prev) - int(total_len * percentage_new))
        except Exception: return None
# [END FILE]
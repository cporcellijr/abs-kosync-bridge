# [START FILE: abs-kosync-enhanced/ebook_utils.py]
"""
Ebook Utilities for abs-kosync-bridge

HARDENED + ENHANCED VERSION with:
- LRU Cache (capacity=3) to prevent OOM
- Robust path resolution
- Rich Locator Support (href + cssSelector + xpath + cfi) for Storyteller/Booklore/KOReader
- New: resolve_locator_id() for Storyteller/Readium-style locators (#id fragments)
- Improved hashing options
- ID-anchored XPath for better robustness
"""

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, Tag
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
    def __init__(self, books_dir):
        self.books_dir = Path(books_dir)
        cache_size = int(os.getenv("EBOOK_CACHE_SIZE", 3))
        self.cache = LRUCache(capacity=cache_size)
        self.fuzzy_threshold = int(os.getenv("FUZZY_MATCH_THRESHOLD", 80))
        self.hash_method = os.getenv("KOSYNC_HASH_METHOD", "content").lower()
        logger.info(f"EbookParser initialized (cache={cache_size}, hash={self.hash_method})")

    def _resolve_book_path(self, filename):
        try:
            safe_name = glob.escape(filename)
            return next(self.books_dir.glob(f"**/{safe_name}"))
        except StopIteration:
            pass
        for f in self.books_dir.rglob("*"):
            if f.name == filename:
                return f
        raise FileNotFoundError(f"Could not locate {filename}")

    def get_kosync_id(self, filepath):
        filepath = Path(filepath)
        if self.hash_method == "filename":
            return hashlib.md5(filepath.name.encode('utf-8')).hexdigest()
        # Fast content-based hash (first 4KB) – simpler and faster than KOReader's multi-offset method
        try:
            with open(filepath, 'rb') as f:
                return hashlib.md5(f.read(4096)).hexdigest()
        except Exception as e:
            logger.error(f"Error computing hash for {filepath}: {e}")
            return None

    def extract_text_and_map(self, filepath):
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

    # --- NEW METHOD: Resolve Storyteller/Readium Locator (href + #id) ---
    def resolve_locator_id(self, filename, href, fragment_id):
        """
        Returns a text snippet starting at the element identified by href + #fragment_id.
        Useful for syncing from Storyteller or any Readium-based reader that uses DOM IDs.
        """
        try:
            book_path = self._resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            # Normalize href matching (Storyteller may send full OEBPS path, internal is often relative)
            target_item = None
            for item in spine_map:
                if href in item['href'] or item['href'] in href:
                    target_item = item
                    break

            if not target_item:
                logger.warning(f"Could not find spine item matching href: {href}")
                return None

            soup = BeautifulSoup(target_item['content'], 'html.parser')
            clean_id = fragment_id.lstrip('#')
            element = soup.find(id=clean_id)

            if not element:
                logger.warning(f"Found chapter {href} but no element with id='{clean_id}'")
                return None

            elem_text = element.get_text(separator=' ', strip=True)
            if not elem_text:
                return None

            chapter_text = BeautifulSoup(target_item['content'], 'html.parser').get_text(separator=' ', strip=True)
            local_offset = chapter_text.find(elem_text)
            if local_offset == -1:
                return None

            global_offset = target_item['start'] + local_offset

            snippet_len = 500
            start = max(0, global_offset)
            end = min(len(full_text), global_offset + snippet_len)
            return full_text[start:end]

        except Exception as e:
            logger.error(f"Error resolving locator ID {fragment_id} in {filename}: {e}")
            return None

    def _generate_css_selector(self, target_tag):
        """Generate a Readium-compatible CSS selector."""
        if not target_tag:
            return ""
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
        """Generate an EPUB CFI for Booklore."""
        soup = BeautifulSoup(html_content, 'html.parser')
        current_char_count = 0
        target_tag = None
        char_offset = 0

        elements = soup.find_all(string=True)
        for string in elements:
            text_len = len(string.strip())
            if text_len == 0:
                continue
            if current_char_count + text_len >= local_target_index:
                target_tag = string.parent
                char_offset = local_target_index - current_char_count
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

    def _generate_xpath(self, html_content, local_target_index):
        """
        Generate robust XPath using ID anchoring when possible.
        Returns: (xpath_string, target_tag_object, is_anchored)
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        current_char_count = 0
        target_tag = None

        elements = soup.find_all(string=True)
        for string in elements:
            text_len = len(string.strip())
            if text_len == 0:
                continue
            if current_char_count + text_len >= local_target_index:
                target_tag = string.parent
                break
            current_char_count += text_len
            if current_char_count < local_target_index:
                current_char_count += 1

        if not target_tag:
            return "/body/div/p[1]", None, False

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

        if found_anchor:
            xpath = "//" + "/".join(reversed(path_segments))
        else:
            xpath = "/" + "/".join(reversed(path_segments))

        return xpath, target_tag, found_anchor

    def _normalize(self, text):
        return re.sub(r'[^a-z0-9]', '', text.lower())

    def find_text_location(self, filename, search_phrase, hint_percentage=None):
        """
        Find text location using exact → normalized → fuzzy matching.
        Returns: (percentage, rich_locator_dict) or (None, None)
        """
        try:
            book_path = self._resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)

            if not full_text:
                return None, None

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
                    if alignment:
                        match_index = w_start + alignment.dest_start

                if match_index == -1:
                    alignment = rapidfuzz.fuzz.partial_ratio_alignment(
                        search_phrase, full_text, score_cutoff=cutoff
                    )
                    if alignment:
                        match_index = alignment.dest_start

            if match_index != -1:
                percentage = match_index / total_len

                for item in spine_map:
                    if item['start'] <= match_index < item['end']:
                        local_index = match_index - item['start']

                        xpath_str, target_tag, is_anchored = self._generate_xpath(item['content'], local_index)
                        css_selector = self._generate_css_selector(target_tag)
                        cfi = self._generate_cfi(item['spine_index'] - 1, item['content'], local_index)

                        doc_frag_prefix = f"/body/DocFragment[{item['spine_index']}]"
                        if is_anchored:
                            final_xpath = f"{doc_frag_prefix}{xpath_str}"
                        else:
                            final_xpath = f"{doc_frag_prefix}{xpath_str}"

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

    def get_text_at_percentage(self, filename, percentage):
        try:
            book_path = self._resolve_book_path(filename)
            full_text, _ = self.extract_text_and_map(book_path)
            if not full_text:
                return None

            target_index = int(len(full_text) * percentage)
            start = max(0, target_index - 450)
            end = min(len(full_text), target_index + 450)
            return full_text[start:end]
        except Exception:
            return None

    def get_character_delta(self, filename, percentage_prev, percentage_new):
        try:
            book_path = self._resolve_book_path(filename)
            full_text, _ = self.extract_text_and_map(book_path)
            if not full_text:
                return None
            total_len = len(full_text)
            return abs(int(total_len * percentage_prev) - int(total_len * percentage_new))
        except Exception:
            return None

# [END FILE]
"""
Ebook Utilities for abs-kosync-bridge

HARDENED VERSION with:
- LRU Cache (capacity=3) to prevent OOM from caching all books
- Robust path resolution for special characters
- Multiple matching strategies (exact, normalized, fuzzy)
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
from rapidfuzz import process, fuzz
from collections import OrderedDict

logger = logging.getLogger(__name__)


class LRUCache:
    """
    Least Recently Used cache with fixed capacity.
    Automatically evicts oldest entries when capacity is exceeded.
    """
    
    def __init__(self, capacity: int = 3):
        self.cache = OrderedDict()
        self.capacity = capacity

    def get(self, key):
        if key not in self.cache:
            return None
        # Move to end (most recently used)
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key, value):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        # Evict oldest if over capacity
        while len(self.cache) > self.capacity:
            evicted_key, _ = self.cache.popitem(last=False)
            logger.debug(f"LRU evicted: {evicted_key}")
    
    def clear(self):
        self.cache.clear()


class EbookParser:
    def __init__(self, books_dir):
        self.books_dir = Path(books_dir)
        
        # LRU Cache - keeps only last N books in memory to prevent OOM
        cache_size = int(os.getenv("EBOOK_CACHE_SIZE", 3))
        self.cache = LRUCache(capacity=cache_size)
        
        self.fuzzy_threshold = int(os.getenv("FUZZY_MATCH_THRESHOLD", 80))
        self.hash_method = os.getenv("KOSYNC_HASH_METHOD", "content").lower()
        
        logger.info(f"EbookParser initialized (cache={cache_size}, hash={self.hash_method})")

    def _resolve_book_path(self, filename):
        """
        Robustly finds a file in the books directory.
        Handles special characters like [ ] which break standard glob.
        """
        # Strategy 1: Glob with escaping (fastest)
        try:
            safe_name = glob.escape(filename)
            return next(self.books_dir.glob(f"**/{safe_name}"))
        except StopIteration:
            pass
        
        # Strategy 2: Linear scan (slower but reliable)
        for f in self.books_dir.rglob("*"):
            if f.name == filename:
                return f
        
        raise FileNotFoundError(f"Could not locate {filename}")

    def get_kosync_id(self, filepath):
        """Generate KoSync document ID using configured hash method."""
        filepath = Path(filepath)
        if self.hash_method == "filename":
            return self._compute_filename_hash(filepath)
        return self._compute_koreader_hash(filepath)

    def _compute_filename_hash(self, filepath):
        return hashlib.md5(filepath.name.encode('utf-8')).hexdigest()

    def _compute_koreader_hash(self, filepath):
        """
        Compute hash exactly as KOReader does.
        Samples specific byte offsets to create a unique fingerprint.
        """
        md5 = hashlib.md5()
        try:
            file_size = os.path.getsize(filepath)
            with open(filepath, 'rb') as f:
                for i in range(-1, 11):
                    if i == -1:
                        offset = 0
                    else:
                        offset = 1024 * (4 ** i)
                    if offset >= file_size:
                        break
                    f.seek(offset)
                    chunk = f.read(1024)
                    if not chunk:
                        break
                    md5.update(chunk)
            return md5.hexdigest()
        except Exception as e:
            logger.error(f"Error computing KOReader hash: {e}")
            return None

    def extract_text_and_map(self, filepath):
        """
        Extract full text and spine map from EPUB.
        Results are cached in LRU cache.
        """
        # Resolve filename to full path if needed
        filepath = Path(filepath)
        if not filepath.exists():
            filepath = self._resolve_book_path(filepath.name)
        str_path = str(filepath)
        
        # Check LRU cache first
        cached = self.cache.get(str_path)
        if cached:
            logger.debug(f"Cache hit: {filepath.name}")
            return cached['text'], cached['map']
        
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
                    
                    spine_map.append({
                        "start": start,
                        "end": end,
                        "spine_index": i + 1,
                        "content": item.get_content()
                    })
                    
                    full_text_parts.append(text)
                    current_idx = end + 1
            
            combined_text = " ".join(full_text_parts)
            
            # Store in LRU cache
            self.cache.put(str_path, {'text': combined_text, 'map': spine_map})
            
            logger.debug(f"Parsed {filepath.name}: {len(combined_text)} chars, {len(spine_map)} spine items")
            return combined_text, spine_map
            
        except Exception as e:
            logger.error(f"Failed to parse EPUB {filepath}: {e}")
            return "", []

    def _generate_xpath(self, html_content, local_target_index):
        """Generate XPath for a character position within HTML content."""
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
            return "/body/div/p[1]"
        
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
        
        return "/" + "/".join(reversed(path_segments))

    def _normalize(self, text):
        """Normalize text for fuzzy matching (lowercase, alphanumeric only)."""
        return re.sub(r'[^a-z0-9]', '', text.lower())

    def find_text_location(self, filename, search_phrase, hint_percentage=None):
        """
        Find the location of search_phrase in the ebook.
        
        Returns: (percentage, xpath, char_index) or (None, None, None)
        
        Args:
            filename: EPUB filename
            search_phrase: Text to find
            hint_percentage: Optional hint for where to search first (optimization)
        """
        try:
            book_path = self._resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)
            
            if not full_text:
                return None, None, None
            
            total_len = len(full_text)
            match_index = -1
            
            # Strategy 1: Exact match
            match_index = full_text.find(search_phrase)
            
            # Strategy 2: Normalized match
            if match_index == -1:
                logger.debug("Trying normalized match...")
                norm_content = self._normalize(full_text)
                norm_search = self._normalize(search_phrase)
                norm_index = norm_content.find(norm_search)
                
                if norm_index != -1:
                    # Map normalized index back to original
                    match_index = int((norm_index / len(norm_content)) * total_len)
                    logger.debug(f"Normalized match at {match_index}")
            
            # Strategy 3: Fuzzy match with RapidFuzz
            if match_index == -1:
                logger.debug("Trying fuzzy match...")
                cutoff_score = self.fuzzy_threshold
                
                # If we have a hint, search nearby first (±10% window)
                if hint_percentage is not None:
                    window_start = int(max(0, hint_percentage - 0.10) * total_len)
                    window_end = int(min(1.0, hint_percentage + 0.10) * total_len)
                    windowed_text = full_text[window_start:window_end]
                    
                    alignment = rapidfuzz.fuzz.partial_ratio_alignment(
                        search_phrase, windowed_text, score_cutoff=cutoff_score
                    )
                    if alignment:
                        match_index = window_start + alignment.dest_start
                        logger.debug(f"Windowed fuzzy match at {match_index}")
                
                # Full text fuzzy search as fallback
                if match_index == -1:
                    alignment = rapidfuzz.fuzz.partial_ratio_alignment(
                        search_phrase, full_text, score_cutoff=cutoff_score
                    )
                    if alignment:
                        match_index = alignment.dest_start
                        logger.debug(f"Full fuzzy match at {match_index}")
            
            if match_index != -1:
                percentage = match_index / total_len
                xpath = None
                
                for item in spine_map:
                    if item['start'] <= match_index < item['end']:
                        local_index = match_index - item['start']
                        dom_path = self._generate_xpath(item['content'], local_index)
                        xpath = f"/body/DocFragment[{item['spine_index']}]{dom_path}"
                        break
                
                return percentage, xpath, match_index
            
            return None, None, None
            
        except FileNotFoundError:
            logger.error(f"Book not found: {filename}")
            return None, None, None
        except Exception as e:
            logger.error(f"Error finding text in {filename}: {e}")
            return None, None, None

    def get_text_at_percentage(self, filename, percentage):
        """
        Extract ~900 characters of text centered at the given percentage.
        """
        try:
            book_path = self._resolve_book_path(filename)
            full_text, _ = self.extract_text_and_map(book_path)
            
            if not full_text:
                return None
            
            total_len = len(full_text)
            target_index = int(total_len * percentage)
            
            # Extract window of text (450 chars before and after)
            start = max(0, target_index - 450)
            end = min(total_len, target_index + 450)
            
            return full_text[start:end]
            
        except FileNotFoundError:
            logger.error(f"Book not found: {filename}")
            return None
        except Exception as e:
            logger.error(f"Error extracting text from {filename}: {e}")
            return None

    def get_character_delta(self, filename, percentage_prev, percentage_new):
        """
        Calculate character count difference between two percentages.
        Used for threshold checking.
        """
        try:
            book_path = self._resolve_book_path(filename)
            full_text, _ = self.extract_text_and_map(book_path)
            
            if not full_text:
                return None
            
            total_len = len(full_text)
            index_prev = int(total_len * percentage_prev)
            index_new = int(total_len * percentage_new)
            
            return abs(index_new - index_prev)
            
        except FileNotFoundError:
            logger.error(f"Book not found: {filename}")
            return None
        except Exception as e:
            logger.error(f"Error calculating delta for {filename}: {e}")
            return None

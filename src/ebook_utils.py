import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString, Tag
import hashlib
import logging
import os
import re
import glob # <--- Added import for escaping
from pathlib import Path
from rapidfuzz import process, fuzz
from fuzzysearch import find_near_matches

logger = logging.getLogger(__name__)

class EbookParser:
    def __init__(self, books_dir):
        self.books_dir = books_dir
        self.cache = {} 
        self.normalized_cache = {}
        self.sentence_cache = {}
        # Stores metadata about where chapters begin/end in the full text
        self.spine_maps = {} 
        
        self.fuzzy_threshold = int(os.getenv("FUZZY_MATCH_THRESHOLD", 80))
        self.hash_method = os.getenv("KOSYNC_HASH_METHOD", "content").lower()
        logger.info(f"Initialized EbookParser. ID Method: {self.hash_method}")

    def _resolve_book_path(self, filename):
        """
        Robustly finds a file in the books directory, handling special characters
        like [ ] (brackets) which break standard glob patterns.
        """
        # 1. Try Glob with escaping (Fastest)
        try:
            # Escape brackets, etc.
            safe_name = glob.escape(filename) 
            return next(self.books_dir.glob(f"**/{safe_name}"))
        except StopIteration:
            pass
            
        # 2. Fallback: Linear scan (Slower, but 100% reliable)
        # This catches edge cases where glob.escape might behave differently on OS versions
        for f in self.books_dir.rglob("*"):
            if f.name == filename:
                return f
        
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
                    if i == -1: offset = 0
                    else: offset = 1024 * (4 ** i)
                    if offset >= file_size: break
                    f.seek(offset)
                    chunk = f.read(1024)
                    if not chunk: break   
                    md5.update(chunk)
            return md5.hexdigest()
        except Exception as e:
            logger.error(f"Error computing hash: {e}")
            return None

    def extract_text_and_map(self, filepath):
        filepath = Path(filepath)
        if str(filepath) in self.cache:
            return self.cache[str(filepath)], self.spine_maps[str(filepath)]

        logger.info(f"Parsing ebook structure: {filepath.name}")
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
            self.cache[str(filepath)] = combined_text
            self.spine_maps[str(filepath)] = spine_map
            
            return combined_text, spine_map
            
        except Exception as e:
            logger.error(f"Failed to parse EPUB {filepath}: {e}")
            return "", []

    def _generate_xpath(self, html_content, local_target_index):
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
            
            segment = f"{curr.name}[{index}]"
            path_segments.append(segment)
            curr = curr.parent
            
        return "/" + "/".join(reversed(path_segments))

    def _normalize(self, text):
        return re.sub(r'[^a-z0-9]', '', text.lower())

    def find_text_location(self, filename, search_phrase):
        try:
            # FIXED: Use the new robust path resolver
            book_path = self._resolve_book_path(filename)
            
            full_text, spine_map = self.extract_text_and_map(book_path)
            
            if not full_text: return None, None

            total_len = len(full_text)
            match_index = -1

            # 1. Exact Match
            match_index = full_text.find(search_phrase)
            
            # 2. Normalized Match
            if match_index == -1:
                logger.info("   ...Exact match failed. Trying Normalized match...")
                cache_key = str(filename)
                if cache_key not in self.normalized_cache:
                    self.normalized_cache[cache_key] = self._normalize(full_text)
                
                norm_content = self.normalized_cache[cache_key]
                norm_search = self._normalize(search_phrase)
                norm_index = norm_content.find(norm_search)

                if norm_index != -1:
                    logger.info("   ✅ Normalized match successful.")
                    match_index = int((norm_index / len(norm_content)) * total_len)

            # # 3. Fuzzy Match
            # if match_index == -1:
            #     logger.info(f"   ...Normalized failed. Trying Fuzzy Match...")
            #     cache_key = str(filename)
            #     if cache_key not in self.sentence_cache:
            #         self.sentence_cache[cache_key] = full_text.split('. ')
                
            #     sentences = self.sentence_cache[cache_key]
            #     match = process.extractOne(search_phrase, sentences, scorer=fuzz.token_set_ratio)
                
            #     if match:
            #         matched_string, score, _ = match
            #         if score >= self.fuzzy_threshold:
            #             logger.info(f"   ✅ Fuzzy match successful (Score: {score:.1f}).")
            #             match_index = full_text.find(matched_string)
           
            # 3. Fuzzy Match (Revised)
            if match_index == -1:
                logger.info("   ...Normalized failed. Trying Fuzzy Match with Levenshtein distance...")
                
                max_errors = int(len(search_phrase) * 0.2) # Allow 20% error rate
                
                matches = find_near_matches(search_phrase, full_text, max_l_dist=max_errors)
            
                if matches:
                    # Get the best match (lowest distance / errors)
                    best_match = min(matches, key=lambda x: x.dist)
                    
                    logger.info(f"   ✅ Fuzzy match successful (Dist: {best_match.dist}).")
                    match_index = best_match.start            

            if match_index != -1:
                percentage = match_index / total_len
                xpath = None
                for item in spine_map:
                    if item['start'] <= match_index < item['end']:
                        local_index = match_index - item['start']
                        dom_path = self._generate_xpath(item['content'], local_index)
                        xpath = f"/body/DocFragment[{item['spine_index']}]{dom_path}"
                        logger.info(f"   📍 Generated XPath: {xpath}")
                        break
                
                return percentage, xpath
            
            return None, None

        except FileNotFoundError:
            logger.error(f"Book file not found: {filename}")
            return None, None
        except Exception as e:
            logger.error(f"Error finding location in {filename}: {e}")
            return None, None

    def get_text_at_percentage(self, filename, percentage):
        try:
            # FIXED: Use the new robust path resolver
            book_path = self._resolve_book_path(filename)
            
            full_text, _ = self.extract_text_and_map(book_path)
            
            if not full_text: return None
            
            total_len = len(full_text)
            target_index = int(total_len * percentage)
            
            start = max(0, target_index - 450)
            end = min(total_len, target_index + 450)
            
            return full_text[start:end]
        except FileNotFoundError:
            logger.error(f"Book file not found: {filename}")
            return None
        except Exception as e:
            logger.error(f"Error extracting text from {filename}: {e}")
            return None

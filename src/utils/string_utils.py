
import re
import difflib

def clean_book_title(title: str) -> str:
    """
    Cleans a book title by removing common subtitles, series info, and extra whitespace.
    
    Examples:
    "Harry Potter and the Sorcerer's Stone (Harry Potter, #1)" -> "Harry Potter and the Sorcerer's Stone"
    "Dune: Deluxe Edition" -> "Dune"
    """
    if not title:
        return ""
    
    # Remove text in parentheses (often series info or edition info)
    title = re.sub(r'\s*\(.*?\)', '', title)
    
    # Remove text after a colon (often subtitles) - debatable, but trying for stickiness to main title
    # For matching purposes, sometimes the subtitle is noise. 
    # Let's be careful: "Dune: Messiah" -> "Dune" might be bad if we want Messiah.
    # But usually Hardcover search is better with fewer words.
    # Let's strip subtitles for now as a "clean" strategy, 
    # but the caller might want to try both raw and clean.
    if ':' in title:
        title = title.split(':')[0]
        
    return title.strip()

def calculate_similarity(a: str, b: str) -> float:
    """
    Calculates the similarity ratio between two strings using SequenceMatcher.
    Returns reduced score if strings are very different in length to punish partial matches on short strings.
    """
    if not a or not b:
        return 0.0
        
    a = a.lower().strip()
    b = b.lower().strip()
    
    return difflib.SequenceMatcher(None, a, b).ratio()

def fuzzy_match_title(query: str, target: str, threshold: float = 0.6) -> bool:
    """
    Check if query title fuzzy matches the target title.
    Uses word-overlap logic with normalization to handle punctuation differences.
    
    Args:
        query: The search term (e.g. from filename)
        target: The target title (e.g. from ABS)
        threshold: Required match percentage (default 0.6 / 60%)
        
    Returns:
        True if it's a match, False otherwise.
    """
    if not query or not target:
        return False
    
    # Normalize: lowercase and remove punctuation except spaces
    def normalize(s):
        return re.sub(r'[^\w\s]', '', s.lower())
    
    query_norm = normalize(query)
    target_norm = normalize(target)
    
    # Check for title match (fuzzy - title words in audiobook title)
    title_words = [w for w in query_norm.split() if len(w) > 3]
    
    if not title_words:
        # Fallback for short titles: exact normalized match
        return query_norm in target_norm
    else:
        matches = sum(1 for w in title_words if w in target_norm)
        # Stricter threshold check
        return (matches / len(title_words) > threshold) or (query_norm in target_norm)

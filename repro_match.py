
import re

def normalize(s):
    if not s: return ""
    return re.sub(r'[^\w\s]', '', s.lower())

search_term = "You Should Have Left - Ross Benjamin, Daniel Kehlmann (2016)"
search_norm = normalize(search_term)
print(f"Search Term: '{search_term}'")
print(f"Search Norm: '{search_norm}'")

test_cases = [
    {"title": "The Country Club", "author": ""},
    {"title": "Red", "author": ""},
    {"title": "Clown Outbreak", "author": " "},
    {"title": "Rape Van", "author": None},
    {"title": "Spankenstein", "author": "Unknown"},
]

print("\n--- Testing Logic ---")
for case in test_cases:
    title = case["title"]
    author = case["author"] or ""
    
    title_norm = normalize(title)
    author_norm = normalize(author)
    
    match = False
    reason = ""
    
    # The logic from the server
    if (search_norm in title_norm or title_norm in search_norm) or (author_norm in search_norm):
        match = True
        
        if search_norm in title_norm: reason = "search in title"
        elif title_norm in search_norm: reason = "title in search"
        elif author_norm in search_norm: reason = f"author '{author_norm}' in search"
        
    print(f"'{title}' by '{author}' -> Match: {match} ({reason})")

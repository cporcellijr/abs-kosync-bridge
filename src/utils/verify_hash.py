import hashlib
import sys
import os

def compute_koreader_hash(filepath):
    """
    Replicates the Lua:Document:fastDigest function from Koreader.
    This is the "Content" based hash.
    
    Logic:
    - It reads 1024-byte chunks at specific offsets.
    - Offsets are calculated via bit.lshift(1024, 2*i) for i in -1..10.
    - In Python: offset = 1024 * (4 ** i) (handling i=-1 as 0)
    """
    md5 = hashlib.md5()
    
    try:
        file_size = os.path.getsize(filepath)
    except FileNotFoundError:
        print(f"Error: File not found: {filepath}")
        return None

    with open(filepath, 'rb') as f:
        # Loop from -1 to 10 (inclusive) matching Koreader's loop
        for i in range(-1, 11): 
            
            # Calculate Offset
            if i == -1:
                # In LuaJIT, lshift(1024, -2) overflows to 0
                offset = 0
            else:
                # 1024 left shifted by 2*i is equivalent to 1024 * (4^i)
                offset = 1024 * (4 ** i)

            # Stop if offset is beyond file size
            if offset >= file_size:
                break

            f.seek(offset)
            chunk = f.read(1024)
            
            if not chunk:
                break
                
            md5.update(chunk)

    return md5.hexdigest()

def compute_filename_hash(filepath):
    """
    Computes MD5 of the filename only (The 'Filename' based method).
    """
    filename = os.path.basename(filepath)
    return hashlib.md5(filename.encode('utf-8')).hexdigest()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python verify_hash.py <path_to_epub>")
        sys.exit(1)

    path = sys.argv[1]
    
    print(f"Analyzing: {os.path.basename(path)}")
    print("-" * 40)
    
    content_hash = compute_koreader_hash(path)
    filename_hash = compute_filename_hash(path)
    
    print(f"1. Content Hash (KoReader FastDigest): {content_hash}")
    print(f"2. Filename Hash (MD5 of filename):    {filename_hash}")
    print("-" * 40)

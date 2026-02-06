import os
import time
from pathlib import Path
from src.api.booklore_client import BookloreClient

# 1. Check BookloreClient methods
client = BookloreClient()
print(f"BookloreClient has download_book: {hasattr(client, 'download_book')}")

# 2. Check epub_cache files
cache_dir = Path(os.environ.get("DATA_DIR", "/data")) / "epub_cache"
print(f"Checking cache dir: {cache_dir}")
if cache_dir.exists():
    files = list(cache_dir.glob("*"))
    print(f"File count: {len(files)}")
    # Print first 5 files with modification time
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    for f in files[:5]:
        mtime = time.ctime(f.stat().st_mtime)
        print(f"  - {f.name}: {mtime}")
else:
    print("Cache dir does not exist")

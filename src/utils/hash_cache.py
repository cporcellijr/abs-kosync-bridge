"""
KOSync Hash Cache with smart invalidation.

Supports:
- Reverse lookup (hash -> filename)
- Filesystem mtime-based invalidation
- Booklore ID tracking
- Manual cache clearing
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any
from threading import Lock

logger = logging.getLogger(__name__)

CACHE_VERSION = 2

class HashCache:
    def __init__(self, cache_file: Path):
        self.cache_file = Path(cache_file)
        self._lock = Lock()
        self._cache = self._load_cache()
    
    def _load_cache(self) -> Dict[str, Any]:
        """Load cache from disk, migrating if necessary."""
        if not self.cache_file.exists():
            return self._empty_cache()
        
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Check version and migrate if needed
            if data.get('version') != CACHE_VERSION:
                logger.info(f"Migrating hash cache from version {data.get('version')} to {CACHE_VERSION}")
                return self._migrate_cache(data)
            
            return data
        except Exception as e:
            logger.warning(f"Failed to load hash cache: {e}")
            return self._empty_cache()
    
    def _empty_cache(self) -> Dict[str, Any]:
        return {
            'version': CACHE_VERSION,
            'by_hash': {},
            'by_booklore_id': {},
            'by_filepath': {}
        }
    
    def _migrate_cache(self, old_data: Dict) -> Dict[str, Any]:
        """Migrate old cache format (v1: {booklore_id: hash}) to new format."""
        new_cache = self._empty_cache()
        
        # Old format was just {booklore_id: hash}
        if 'version' not in old_data:
            for booklore_id, hash_val in old_data.items():
                new_cache['by_booklore_id'][booklore_id] = {
                    'hash': hash_val,
                    'filename': None,  # Unknown in old format
                    'cached_at': time.time()
                }
                new_cache['by_hash'][hash_val] = {
                    'filename': None,
                    'source': 'booklore',
                    'booklore_id': booklore_id,
                    'cached_at': time.time()
                }
        
        self._save_cache(new_cache)
        return new_cache
    
    def _save_cache(self, cache: Dict = None):
        """Save cache to disk."""
        if cache is None:
            cache = self._cache
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save hash cache: {e}")
    
    def lookup_by_hash(self, doc_hash: str) -> Optional[str]:
        """Find filename by KOSync hash. Returns filename or None."""
        with self._lock:
            entry = self._cache.get('by_hash', {}).get(doc_hash)
            if entry and entry.get('filename'):
                return entry['filename']
        return None
    
    def lookup_by_filepath(self, filepath: Path) -> Optional[str]:
        """Get cached hash for filepath, checking mtime for invalidation."""
        filepath = Path(filepath)
        filepath_str = str(filepath)
        
        with self._lock:
            entry = self._cache.get('by_filepath', {}).get(filepath_str)
            if not entry:
                return None
            
            # Check if file still exists and mtime matches
            try:
                current_mtime = filepath.stat().st_mtime
                if entry.get('mtime') == current_mtime:
                    return entry.get('hash')
                else:
                    # File changed, invalidate
                    logger.debug(f"Hash cache invalidated (mtime changed): {filepath.name}")
                    del self._cache['by_filepath'][filepath_str]
                    return None
            except FileNotFoundError:
                # File gone, invalidate
                del self._cache['by_filepath'][filepath_str]
                return None
        
        return None
    
    def lookup_by_booklore_id(self, booklore_id: str) -> Optional[str]:
        """Get cached hash for Booklore book ID."""
        with self._lock:
            entry = self._cache.get('by_booklore_id', {}).get(str(booklore_id))
            if entry:
                return entry.get('hash')
        return None
    
    def store_hash(self, doc_hash: str, filename: str, 
                   source: str = 'filesystem',
                   filepath: Path = None,
                   booklore_id: str = None):
        """Store a hash with all relevant indexes."""
        now = time.time()
        
        with self._lock:
            # Store in by_hash index
            self._cache['by_hash'][doc_hash] = {
                'filename': filename,
                'source': source,
                'booklore_id': booklore_id,
                'cached_at': now
            }
            
            # Store in by_filepath index (with mtime for invalidation)
            if filepath:
                filepath = Path(filepath)
                try:
                    mtime = filepath.stat().st_mtime
                    self._cache['by_filepath'][str(filepath)] = {
                        'hash': doc_hash,
                        'mtime': mtime,
                        'cached_at': now
                    }
                except FileNotFoundError:
                    pass
            
            # Store in by_booklore_id index
            if booklore_id:
                self._cache['by_booklore_id'][str(booklore_id)] = {
                    'hash': doc_hash,
                    'filename': filename,
                    'cached_at': now
                }
            
            self._save_cache()
    
    def invalidate_by_booklore_id(self, booklore_id: str):
        """Invalidate cache entry for a Booklore book (e.g., when metadata changes)."""
        with self._lock:
            entry = self._cache.get('by_booklore_id', {}).get(str(booklore_id))
            if entry:
                old_hash = entry.get('hash')
                del self._cache['by_booklore_id'][str(booklore_id)]
                if old_hash and old_hash in self._cache.get('by_hash', {}):
                    del self._cache['by_hash'][old_hash]
                self._save_cache()
                self._save_cache()
                logger.debug(f"Invalidated cache for Booklore ID: {booklore_id}")
    
    def delete_hash(self, doc_hash: str):
        """Delete a hash entry and all its indexes."""
        with self._lock:
            # Get entry to find other keys (filepath, booklore_id)
            entry = self._cache.get('by_hash', {}).get(doc_hash)
            if not entry:
                return

            # Remove from by_hash
            del self._cache['by_hash'][doc_hash]

            # Remove from by_booklore_id
            booklore_id = entry.get('booklore_id')
            if booklore_id and str(booklore_id) in self._cache.get('by_booklore_id', {}):
                del self._cache['by_booklore_id'][str(booklore_id)]

            # Remove from by_filepath
            # We need to find which filepath points to this hash. 
            # The entry doesn't strictly store the full filepath key used in by_filepath,
            # but usually we can infer it or scan.
            # Scanning is safe since by_filepath isn't huge.
            filepath_to_remove = None
            for fp, f_entry in self._cache.get('by_filepath', {}).items():
                if f_entry.get('hash') == doc_hash:
                    filepath_to_remove = fp
                    break
            
            if filepath_to_remove:
                del self._cache['by_filepath'][filepath_to_remove]

            self._save_cache()
            logger.debug(f"Deleted hash cache entry: {doc_hash}")
    
    def clear(self):
        """Clear entire cache."""
        with self._lock:
            self._cache = self._empty_cache()
            self._save_cache()
            logger.info("Hash cache cleared")
    
    def stats(self) -> Dict[str, int]:
        """Return cache statistics."""
        with self._lock:
            return {
                'by_hash': len(self._cache.get('by_hash', {})),
                'by_filepath': len(self._cache.get('by_filepath', {})),
                'by_booklore_id': len(self._cache.get('by_booklore_id', {}))
            }

"""
JsonDB - Thread/Process-safe JSON file persistence with file locking.

This solves the "split-brain" problem where multiple processes (daemon + web server)
read and write the same JSON files, potentially causing data loss.

Uses fcntl.flock() for advisory locking on Unix systems.
"""

import json
import fcntl
import os
import logging
from pathlib import Path
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class JsonDB:
    """
    File-locked JSON database handler.
    
    Usage:
        db = JsonDB("/data/mapping_db.json")
        data = db.load(default={"mappings": []})
        data["mappings"].append(new_item)
        db.save(data)
    """
    
    def __init__(self, filepath):
        self.filepath = Path(filepath)
        # Ensure parent directory exists
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
    
    @contextmanager
    def _locked_file(self, mode='r'):
        """
        Context manager that acquires an exclusive lock on the file.
        
        For reads: acquires shared lock (LOCK_SH) - multiple readers OK
        For writes: acquires exclusive lock (LOCK_EX) - single writer only
        """
        lock_type = fcntl.LOCK_EX if 'w' in mode else fcntl.LOCK_SH
        
        # Create file if it doesn't exist (for write mode)
        if 'w' in mode and not self.filepath.exists():
            self.filepath.touch()
        
        f = None
        try:
            f = open(self.filepath, mode)
            fcntl.flock(f.fileno(), lock_type)
            yield f
        finally:
            if f:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                f.close()
    
    def load(self, default=None):
        """
        Load JSON data with shared (read) lock.
        Returns default if file doesn't exist or is empty/corrupt.
        """
        if default is None:
            default = {}
        
        if not self.filepath.exists():
            return default
        
        try:
            with self._locked_file('r') as f:
                content = f.read().strip()
                if not content:
                    return default
                return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error in {self.filepath}: {e}")
            return default
        except Exception as e:
            logger.error(f"Failed to load {self.filepath}: {e}")
            return default
    
    def save(self, data):
        """
        Save JSON data with exclusive (write) lock.
        Uses atomic write pattern: write, flush, fsync.
        """
        try:
            with self._locked_file('w') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as e:
            logger.error(f"Failed to save {self.filepath}: {e}")
            return False
    
    def update(self, update_func, default=None):
        """
        Atomic read-modify-write operation.
        
        Usage:
            def add_mapping(data):
                data["mappings"].append(new_item)
                return data
            
            db.update(add_mapping, default={"mappings": []})
        """
        if default is None:
            default = {}
        
        # We need exclusive lock for the entire operation
        if not self.filepath.exists():
            self.filepath.touch()
        
        try:
            with open(self.filepath, 'r+') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    content = f.read().strip()
                    data = json.loads(content) if content else default
                    
                    # Apply the update function
                    data = update_func(data)
                    
                    # Write back
                    f.seek(0)
                    f.truncate()
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return True
        except Exception as e:
            logger.error(f"Failed to update {self.filepath}: {e}")
            return False

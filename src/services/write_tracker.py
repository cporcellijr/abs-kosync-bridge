"""
Write-suppression tracker — prevents self-triggered feedback loops.

Call record_write(client_name, abs_id) after BookBridge successfully pushes
progress to any client. Call is_own_write(client_name, abs_id) before acting
on a progress change from that client to suppress round-trip echoes.

Supported client_name values: 'ABS', 'Storyteller', 'BookLore', 'KoSync'
"""

import threading
import time

_recent_writes: dict[str, float] = {}
_writes_lock = threading.Lock()

_DEFAULT_SUPPRESSION_WINDOW = 60  # seconds


def record_write(client_name: str, abs_id: str) -> None:
    """Call after BookBridge successfully pushes progress to a client."""
    key = f"{client_name}:{abs_id}"
    with _writes_lock:
        _recent_writes[key] = time.time()


def is_own_write(client_name: str, abs_id: str, suppression_window: int = _DEFAULT_SUPPRESSION_WINDOW) -> bool:
    """Return True if a recent progress event for this client/book was caused by our own write."""
    key = f"{client_name}:{abs_id}"
    with _writes_lock:
        last_write = _recent_writes.get(key)
        if last_write and time.time() - last_write < suppression_window:
            return True
        # Clean up stale entries while holding the lock
        stale = [k for k, v in _recent_writes.items() if time.time() - v > suppression_window]
        for k in stale:
            del _recent_writes[k]
        return False

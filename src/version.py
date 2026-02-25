import os
import requests
import logging
import time

APP_VERSION = os.environ.get("APP_VERSION", "dev")
_update_cache = None
_last_check = 0
_CHECK_INTERVAL = 86400  # 24 hours

logger = logging.getLogger(__name__)


def get_update_status():
    """Returns (latest_version, update_available) — refreshes every 24 hours."""
    global _update_cache, _last_check
    now = time.time()

    if _update_cache is not None and (now - _last_check) < _CHECK_INTERVAL:
        return _update_cache

    try:
        r = requests.get(
            "https://api.github.com/repos/cporcellijr/abs-kosync-bridge/releases/latest",
            timeout=5,
            headers={"Accept": "application/vnd.github+json"}
        )
        if r.status_code == 200:
            latest = r.json().get("tag_name", "").lstrip("v")
            is_dev = APP_VERSION.startswith("dev")
            available = (latest != APP_VERSION) and not is_dev

            _update_cache = (latest, available)
            _last_check = now
            logger.debug(f"Update check: current={APP_VERSION}, latest={latest}, update_available={available}")
            return _update_cache

    except Exception as e:
        logger.debug(f"Update check failed: {e}")
        if _update_cache is not None:
            return _update_cache

    _update_cache = (None, False)
    _last_check = now
    return _update_cache

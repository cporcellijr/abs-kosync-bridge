import logging
import time
from functools import wraps

logger = logging.getLogger(__name__)


def sanitize_log_data(data):
    """Truncate long strings to "First 50... [truncated] ...Last 50"."""
    if data is None:
        return ""
    try:
        s = str(data)
    except Exception:
        return "[unrepresentable]"
    if len(s) <= 100:
        return s
    return f"{s[:50]}... [truncated] ...{s[-50:]}"


def time_execution(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        end = time.time()
        ms = int((end - start) * 1000)
        try:
            logger.info(f"⏱️ [{func.__name__}] took {ms}ms")
        except Exception:
            pass
        return result
    return wrapper

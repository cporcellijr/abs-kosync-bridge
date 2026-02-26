"""
storygraph_push.py - Fire-and-forget progress push to the StoryGraph sidecar.

Called after a successful sync cycle. If the sidecar is unreachable,
we log a warning and continue - this must never block core sync.
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

_SIDECAR_URL = None


def _get_sidecar_url() -> str | None:
    global _SIDECAR_URL
    if _SIDECAR_URL is None:
        _SIDECAR_URL = os.environ.get("STORYGRAPH_SIDECAR_URL", "").rstrip("/")
    return _SIDECAR_URL or None


def push_progress(abs_id: str, title: str, author: str | None = None,
                  percentage: float = 0.0, is_finished: bool = False) -> None:
    """
    Push current progress to the StoryGraph sidecar queue.
    Safe to call on every sync cycle - the sidecar deduplicates.

    Args:
        abs_id: The ABS book ID
        title: Book title (required for StoryGraph search)
        author: Book author (optional - sidecar can search by title alone)
        percentage: Progress as 0.0-1.0 (will be converted to 0-100 for sidecar)
        is_finished: Whether the book is completed
    """
    url = _get_sidecar_url()
    if not url:
        return

    # Convert 0-1 range to 0-100 range for sidecar
    # Bridge uses 0.0-1.0, sidecar expects 0-100
    if percentage <= 1.0:
        pct_0_100 = round(percentage * 100, 2)
    else:
        # Already in 0-100 range
        pct_0_100 = round(percentage, 2)

    try:
        resp = requests.post(
            f"{url}/progress",
            json={
                "abs_id": abs_id,
                "title": title,
                "author": author,
                "percentage": pct_0_100,
                "is_finished": is_finished,
            },
            timeout=3,
        )
        if resp.status_code != 200:
            logger.warning(
                f"⚠️ StoryGraph sidecar returned {resp.status_code} for '{title}'"
            )
    except requests.exceptions.ConnectionError:
        logger.debug("StoryGraph sidecar not reachable - skipping push")
    except Exception as e:
        logger.warning(f"⚠️ StoryGraph sidecar push failed for '{title}': {e}")

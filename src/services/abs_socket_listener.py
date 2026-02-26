"""
ABS Socket.IO Listener — real-time progress sync via Audiobookshelf websocket.

Connects to ABS as a Socket.IO client, listens for `user_item_progress_updated`
events, and triggers instant sync with debounce to avoid hammering downstream
services during active playback.
"""

import logging
import os
import threading
import time

import requests
import socketio

from src.services.write_tracker import record_write, is_own_write as _tracker_is_own_write

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Write-suppression tracker — delegates to the shared write_tracker module.
# Backward-compatible wrappers kept so abs_sync_client import still works.
# ---------------------------------------------------------------------------


def record_abs_write(abs_id: str) -> None:
    """Call after BookBridge successfully pushes progress to ABS."""
    record_write('ABS', abs_id)


def is_own_write(abs_id: str, suppression_window: int = 60) -> bool:
    """Return True if a recent ABS progress event was caused by our own write."""
    return _tracker_is_own_write('ABS', abs_id, suppression_window)


class ABSSocketListener:
    """Persistent Socket.IO connection to Audiobookshelf for real-time sync."""

    def __init__(
        self,
        abs_server_url: str,
        abs_api_token: str,
        database_service,
        sync_manager,
    ):
        self._server_url = abs_server_url.rstrip("/").replace("/api", "")
        self._api_token = abs_api_token
        self._socket_token: str | None = None
        self._db = database_service
        self._sync_manager = sync_manager

        self._debounce_window = int(
            os.environ.get("ABS_SOCKET_DEBOUNCE_SECONDS", "30")
        )

        # {abs_id: last_event_timestamp}
        self._pending: dict[str, float] = {}
        # Track which abs_ids already had a sync fired for the current event
        self._fired: set[str] = set()
        self._lock = threading.Lock()
        self._auth_retried: bool = False

        self._sio = socketio.Client(
            reconnection=True,
            logger=False,
            engineio_logger=False,
        )
        self._register_handlers()

    # ------------------------------------------------------------------
    # Token acquisition
    # ------------------------------------------------------------------

    @staticmethod
    def _describe_token(token: str) -> str:
        """Return a safe diagnostic string for a token (type + masked preview)."""
        if not token:
            return "<empty>"
        kind = "JWT" if token.startswith("eyJ") else "legacy"
        if len(token) > 12:
            preview = f"{token[:6]}...{token[-4:]}"
        else:
            preview = "***"
        return f"{kind} len={len(token)} [{preview}]"

    def _acquire_socket_token(self) -> str | None:
        """
        Exchange the API Key for a socket-compatible user token.

        ABS v2.26.0+ API Keys (JWT with type:"api") work for REST API calls
        but are not accepted by the Socket.IO auth handler. The user's legacy
        token (stored in the user object) IS accepted.

        Returns None if the exchange fails after all retries.
        """
        logger.debug(
            f"ABS Socket.IO: API token is {self._describe_token(self._api_token)}"
        )
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                url = f"{self._server_url}/api/me"
                resp = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    timeout=15,
                )
                if resp.status_code == 200:
                    user_data = resp.json()
                    username = user_data.get("username", "unknown")
                    abs_type = user_data.get("type", "unknown")
                    legacy_token = user_data.get("token")
                    logger.debug(
                        f"ABS Socket.IO: /api/me returned user='{username}' "
                        f"type='{abs_type}' "
                        f"token={self._describe_token(legacy_token) if legacy_token else '<missing>'}"
                    )
                    if legacy_token and legacy_token != self._api_token:
                        logger.info("🔌 ABS Socket.IO: Acquired user token for socket auth")
                        return legacy_token
                    logger.info("🔌 ABS Socket.IO: Using API token directly (same as user token)")
                    return self._api_token
                else:
                    logger.warning(f"⚠️ ABS Socket.IO: /api/me returned {resp.status_code}")
            except Exception as e:
                logger.warning(f"⚠️ ABS Socket.IO: Token exchange attempt {attempt}/{max_retries} failed — {e}")
                if attempt < max_retries:
                    time.sleep(5 * attempt)

        logger.error("❌ ABS Socket.IO: Could not acquire socket token after retries — listener will not start")
        return None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        sio = self._sio

        @sio.event
        def connect():
            self._auth_retried = False
            token = self._socket_token or self._api_token
            logger.info(
                f"🔌 ABS Socket.IO: Connected — sending auth "
                f"({self._describe_token(token)})"
            )
            sio.emit("auth", token)

        @sio.event
        def disconnect():
            logger.warning("⚠️ ABS Socket.IO: Disconnected (will auto-reconnect)")

        @sio.on("init")
        def on_init(data):
            username = "unknown"
            if isinstance(data, dict):
                user = data.get("user", {})
                if isinstance(user, dict):
                    username = user.get("username", "unknown")
            self._auth_retried = False  # Reset for future reconnects
            logger.info(f"🔌 ABS Socket.IO: Authenticated as '{username}'")

        @sio.on("auth_failed")
        def on_auth_failed(*args):
            token = self._socket_token or self._api_token
            logger.warning(
                f"⚠️ ABS Socket.IO: Authentication failed — "
                f"token was {self._describe_token(token)}. "
                f"API key is {self._describe_token(self._api_token)}."
            )
            if not self._auth_retried and self._socket_token and self._socket_token != self._api_token:
                self._auth_retried = True
                logger.info("🔌 ABS Socket.IO: Legacy token rejected — retrying auth with API key")
                sio.emit("auth", self._api_token)
            else:
                logger.error(
                    "❌ ABS Socket.IO: All auth methods exhausted. "
                    "Check ABS_KEY or try restarting ABS."
                )

        @sio.on("connect_error")
        def on_connect_error(data=None):
            logger.debug("ABS Socket.IO: Connection error (auto-reconnect will handle it)")

        @sio.on("user_item_progress_updated")
        def on_progress_updated(data):
            self._handle_progress_event(data)

    def _handle_progress_event(self, data: dict) -> None:
        """Record a progress event in the debounce dict if it belongs to an active book."""
        if not isinstance(data, dict):
            return

        # ABS event structure: {id, sessionId, deviceDescription, data: {libraryItemId, ...}}
        # The `id` at top level is the mediaProgress record ID (not useful).
        # The actual book ID (`libraryItemId`) is inside the nested `data` dict.
        library_item_id = None

        # Check nested `data` dict first (modern ABS format)
        inner = data.get("data", {})
        if isinstance(inner, dict):
            library_item_id = inner.get("libraryItemId") or inner.get("mediaItemId")

        # Fallback to top-level fields (older ABS format)
        if not library_item_id:
            library_item_id = data.get("libraryItemId") or data.get("mediaItemId")

        if not library_item_id:
            logger.debug("ABS Socket.IO: Progress event missing libraryItemId — ignoring")
            return

        # Check if this is an active book in our database
        book = self._db.get_book(library_item_id)
        if not book or book.status != "active":
            logger.debug(f"ABS Socket.IO: Progress event for '{library_item_id[:12]}...' — not an active book, ignoring")
            return

        with self._lock:
            self._pending[library_item_id] = time.time()
            self._fired.discard(library_item_id)

        logger.debug(f"ABS Socket.IO: Progress event recorded for '{book.abs_title}'")

    # ------------------------------------------------------------------
    # Debounce loop
    # ------------------------------------------------------------------

    def _debounce_loop(self) -> None:
        """Check pending events every 10s and fire sync after debounce window."""
        logger.debug("ABS Socket.IO: Debounce loop started")
        while self._sio.connected or True:
            try:
                time.sleep(10)
                self._check_and_fire()
            except Exception as e:
                logger.debug(f"ABS Socket.IO: Debounce loop error: {e}")

    def _check_and_fire(self) -> None:
        """Fire sync for any books whose debounce window has elapsed."""
        now = time.time()
        to_fire: list[str] = []

        with self._lock:
            for abs_id, last_event in list(self._pending.items()):
                if abs_id in self._fired:
                    continue
                if now - last_event > self._debounce_window:
                    to_fire.append(abs_id)

            for abs_id in to_fire:
                self._fired.add(abs_id)
                del self._pending[abs_id]

        for abs_id in to_fire:
            book = self._db.get_book(abs_id)
            title = book.abs_title if book else abs_id[:12]
            if is_own_write(abs_id):
                logger.debug(f"ABS Socket.IO: Ignoring self-triggered event for '{title}'")
                continue
            logger.info(f"⚡ Socket.IO: ABS progress changed for '{title}' — triggering sync")
            threading.Thread(
                target=self._sync_manager.sync_cycle,
                kwargs={"target_abs_id": abs_id},
                daemon=True,
            ).start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect and block. Call from a daemon thread."""
        logger.info(f"🔌 ABS Socket.IO: Connecting to {self._server_url}")

        # Acquire a socket-compatible token before connecting
        self._socket_token = self._acquire_socket_token()
        if not self._socket_token:
            logger.error("❌ ABS Socket.IO: No valid token — listener will not start")
            return

        try:
            self._sio.start_background_task(self._debounce_loop)
            self._sio.connect(
                self._server_url,
                transports=["websocket"],
                headers={"Authorization": f"Bearer {self._socket_token}"},
                auth={"token": self._socket_token},
            )
            self._sio.wait()
        except Exception as e:
            logger.error(f"❌ ABS Socket.IO: Failed to connect — {e}")

    def stop(self) -> None:
        """Disconnect cleanly."""
        try:
            if self._sio.connected:
                self._sio.disconnect()
                logger.info("🔌 ABS Socket.IO: Disconnected")
        except Exception as e:
            logger.debug(f"ABS Socket.IO: Error during disconnect: {e}")

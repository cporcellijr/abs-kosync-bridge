"""
Readest cloud publish service.

Publishes one local EPUB into the signed-in user's Readest cloud library and
files it into a named "group" (Readest's shelf/collection concept), so a book
already in the BookBridge-managed catalog shows up in the user's Readest app
without them re-uploading it by hand.

This is a one-shot "publish" operation, distinct from the ongoing highlight
sync in `readest_annotation_sync.py` — it uploads the book file (and, best
effort, its cover) and writes/updates the book's sync row, but does not touch
notes/highlights.

Only EPUB is supported in this phase; other formats are reported as skipped.
"""

import logging
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ebooklib import epub

from src.api.readest_client import ReadestClient
from src.utils.ebook_utils import EbookParser
from src.utils.user_config import user_setting

logger = logging.getLogger(__name__)

_DEFAULT_GROUP_NAME = "BookBridge"


def _read_epub_dc_metadata(path: Path) -> tuple[str, str, list[str]]:
    """Read raw Dublin Core title/author/identifiers straight off an EPUB.

    Readest's ``metaHash`` is computed from the EPUB's raw DC identifier
    strings (``uuid:...``, ``calibre:11``, ``ASIN:B087...``) — verified live by
    reproducing Readest's own stored ``meta_hash`` byte-for-byte from the OPF
    title + creators + raw identifiers. `EbookParser.get_book_metadata` is not
    usable here: it normalises and discards the uuid/calibre identifiers,
    keeping only isbn/asin.

    Returns ``(title, author, identifiers)``. ``identifiers`` are the raw,
    stripped ``DC:identifier`` values in document order — NOT normalised,
    filtered, or de-duplicated; `ReadestClient.compute_meta_hash` does its own
    scheme selection. Never raises: any failure logs a warning and returns
    ``("", "", [])``.
    """
    try:
        book = epub.read_epub(str(path))

        title = ""
        for value, _attrs in book.get_metadata("DC", "title"):
            if value and value.strip():
                title = value.strip()
                break

        author = ""
        for value, _attrs in book.get_metadata("DC", "creator"):
            if value and value.strip():
                author = value.strip()
                break

        identifiers = [
            value.strip()
            for value, _attrs in book.get_metadata("DC", "identifier")
            if value and value.strip()
        ]

        return title, author, identifiers
    except Exception as e:
        logger.warning(
            "Readest publish: could not read EPUB DC metadata from %s: %s",
            path, e, exc_info=True,
        )
        return "", "", []


def _iso_to_epoch_ms(timestamp: Optional[str]) -> Optional[int]:
    """Convert a Readest/Supabase ISO-8601 timestamp string to epoch milliseconds.

    Tolerant of a trailing ``Z`` and of fractional seconds (e.g.
    ``2026-08-28T13:09:40.429+00:00``). Returns None for blank or
    unparseable input rather than raising, since these values come straight
    off a pulled server row and are not guaranteed present.
    """
    if not timestamp:
        return None
    raw = timestamp.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


@dataclass
class ReadestPublishResult:
    """Outcome of a `ReadestUploadService.publish_book` call."""

    status: str
    book_hash: Optional[str] = None
    message: str = ""
    usage: Optional[int] = None
    quota: Optional[int] = None


class ReadestUploadService:
    """Publishes local EPUBs into a user's Readest cloud library.

    Reads no settings at construction time — every setting lookup happens
    per call inside `publish_book` so the settings UI applies without a
    restart, per this repo's settings convention.
    """

    def __init__(
        self,
        readest_client: ReadestClient,
        ebook_parser: EbookParser,
        database_service: Optional[Any] = None,
    ) -> None:
        self._client = readest_client
        self._ebook_parser = ebook_parser
        self._db = database_service

    def publish_book(
        self,
        ebook_filename: str,
        title: Optional[str] = None,
        author: Optional[str] = None,
        identifiers: Optional[list[str]] = None,
        group_name: Optional[str] = None,
    ) -> ReadestPublishResult:
        """Upload `ebook_filename` (an EPUB) to Readest and file it into a group.

        `title`, `author`, and `identifiers` are optional: any left as `None`
        (the default) are derived from the EPUB's own Dublin Core metadata
        once the file is resolved. An explicitly supplied empty string/list
        counts as supplied — only `None` triggers derivation for that field.

        Defensive by design: any unexpected failure is caught here and
        reported as an `"error"` result rather than raised, so one book
        failing never breaks a caller iterating a batch.
        """
        try:
            return self._publish_book(ebook_filename, title, author, identifiers, group_name)
        except Exception as e:
            logger.error(
                "Readest publish_book: unexpected failure publishing %s: %s",
                ebook_filename, e, exc_info=True,
            )
            return ReadestPublishResult(
                status="error",
                message=f"Unexpected error publishing {ebook_filename}",
            )

    def _publish_book(
        self,
        ebook_filename: str,
        title: Optional[str],
        author: Optional[str],
        identifiers: Optional[list[str]],
        group_name: Optional[str],
    ) -> ReadestPublishResult:
        if not self._client.is_configured():
            return ReadestPublishResult(status="disabled", message="Readest is not configured")

        try:
            resolved = self._ebook_parser.resolve_book_path(ebook_filename)
        except FileNotFoundError:
            return ReadestPublishResult(
                status="error", message=f"Could not locate {ebook_filename} on disk"
            )
        path = Path(resolved)
        if not path.is_file():
            return ReadestPublishResult(
                status="error", message=f"Resolved path for {ebook_filename} is not a file"
            )

        if path.suffix.lower() != ".epub":
            logger.info(
                "Readest publish skipped for %s: format %s is not supported yet",
                ebook_filename, path.suffix or "(none)",
            )
            return ReadestPublishResult(
                status="skipped",
                message=f"Unsupported format for Readest: {path.suffix or 'unknown'}",
            )

        if title is None or author is None or identifiers is None:
            derived_title, derived_author, derived_identifiers = _read_epub_dc_metadata(path)
            if title is None:
                # A book with no title renders as a blank card in Readest.
                title = derived_title or path.stem
            if author is None:
                author = derived_author
            if identifiers is None:
                identifiers = derived_identifiers

        book_hash = self._client.compute_book_hash(path)
        if not book_hash:
            return ReadestPublishResult(status="error", message=f"Could not hash {ebook_filename}")

        effective_group_name = (group_name or "").strip()
        if not effective_group_name:
            effective_group_name = (user_setting("READEST_GROUP_NAME") or "").strip() or _DEFAULT_GROUP_NAME

        server_row = self._pull_server_row(book_hash)
        already_uploaded = self._book_bytes_present(server_row, book_hash)

        if already_uploaded:
            existing_group = (server_row.get("group_name") or "").strip() if server_row else ""
            if existing_group == effective_group_name:
                logger.info(
                    "Readest publish skipped for %s: already uploaded and filed in group %r",
                    ebook_filename, effective_group_name,
                )
                return ReadestPublishResult(
                    status="skipped",
                    book_hash=book_hash,
                    message="Already present in Readest cloud with the correct group",
                )

        usage: Optional[int] = None
        quota: Optional[int] = None
        uploaded_this_run = False

        if not already_uploaded:
            data = path.read_bytes()
            upload_result = self._client.upload_file(
                self._client.book_file_name(book_hash, "EPUB"), data, book_hash
            )
            if not upload_result:
                logger.info(
                    "Readest publish: book upload failed for %s (hash=%s) — "
                    "see the preceding warning for quota/storage detail",
                    ebook_filename, book_hash,
                )
                return ReadestPublishResult(
                    status="error",
                    book_hash=book_hash,
                    message="Failed to upload book file to Readest",
                )
            uploaded_this_run = True
            usage = upload_result.get("usage")
            quota = upload_result.get("quota")

        cover_hash, cover_uploaded_this_run = self._publish_cover(path, book_hash, ebook_filename)

        meta_hash = self._client.compute_meta_hash(title, [author] if author else [], identifiers or [])
        record: dict[str, Any] = {
            "hash": book_hash,
            "bookHash": book_hash,
            "metaHash": meta_hash,
            "format": "EPUB",
            "title": title,
            "sourceTitle": title,
            "author": author,
        }

        existing_group_name = (server_row.get("group_name") or "").strip() if server_row else ""
        if existing_group_name:
            # The user may have re-filed this book into a different group
            # themselves inside Readest — never stomp that.
            record["groupName"] = existing_group_name
            record["groupId"] = server_row.get("group_id")
            logger.info(
                "Readest publish: carrying over existing group %r for %s (not overriding the user's own filing)",
                existing_group_name, ebook_filename,
            )
        else:
            record["groupName"] = effective_group_name
            record["groupId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, effective_group_name))
            logger.info(
                "Readest publish: filing %s into group %r", ebook_filename, effective_group_name
            )

        # ------------------------------------------------------------------
        # CRITICAL CARRY-OVER: the Readest server nulls any book field absent
        # from a pushed record. Confirmed live against the production API — a
        # push that omitted `progress` silently wiped the user's reading
        # position to null, while `readingStatus` survived because it
        # resolves on its own field-level merge clock (see push_books'
        # docstring in readest_client.py). So whenever a server row already
        # exists we MUST copy its progress, reading status, and (when this
        # run did not just refresh the cover itself) cover fields forward
        # into the new record — omitting them here would erase real user
        # data even though this call only meant to touch upload/group state.
        # ------------------------------------------------------------------
        if server_row:
            if server_row.get("progress") is not None:
                record["progress"] = server_row.get("progress")
            if server_row.get("reading_status"):
                record["readingStatus"] = server_row.get("reading_status")
                record["readingStatusUpdatedAt"] = _iso_to_epoch_ms(
                    server_row.get("reading_status_updated_at")
                )

        now_ms = int(time.time() * 1000)

        if cover_uploaded_this_run:
            record["coverHash"] = cover_hash
            record["coverUpdatedAt"] = now_ms
        elif server_row:
            if server_row.get("cover_hash"):
                record["coverHash"] = server_row.get("cover_hash")
            if server_row.get("cover_updated_at"):
                record["coverUpdatedAt"] = _iso_to_epoch_ms(server_row.get("cover_updated_at"))

        created_at = _iso_to_epoch_ms(server_row.get("created_at")) if server_row else None
        record["createdAt"] = created_at if created_at is not None else now_ms
        record["updatedAt"] = now_ms
        if uploaded_this_run:
            record["uploadedAt"] = now_ms
        else:
            record["uploadedAt"] = _iso_to_epoch_ms(server_row.get("uploaded_at")) if server_row else None
        record["metadataUpdatedAt"] = now_ms
        record["deletedAt"] = None

        if not self._client.push_books([record]):
            return ReadestPublishResult(
                status="error",
                book_hash=book_hash,
                message="Failed to push book record to Readest",
                usage=usage,
                quota=quota,
            )

        status = "updated" if server_row else "created"
        logger.info(
            "Readest publish: %s %s (hash=%s, group=%s)",
            status, ebook_filename, book_hash, record.get("groupName"),
        )
        return ReadestPublishResult(
            status=status,
            book_hash=book_hash,
            message=f"Book {status} in Readest cloud",
            usage=usage,
            quota=quota,
        )

    def _pull_server_row(self, book_hash: str) -> Optional[dict]:
        """Return the pulled Readest book row matching `book_hash`, or None."""
        rows = self._client.pull_books(since=0, book_hash=book_hash)
        if not rows:
            return None
        return next((row for row in rows if row.get("book_hash") == book_hash), None)

    def _book_bytes_present(self, server_row: Optional[dict], book_hash: str) -> bool:
        """Decide whether the book's bytes are actually already in cloud storage.

        `uploaded_at` on the server row is NOT proof by itself — a file
        deleted through Readest's own storage UI leaves that flag stale-true,
        and trusting it blindly means we never re-upload a book that now
        404s on download. The only reliable check is asking storage directly
        via `list_files` for an entry that isn't just the cover blob. If that
        lookup itself fails (returns None), we fall back to trusting
        `uploaded_at` rather than re-uploading blindly on a transient error.
        """
        if not server_row or server_row.get("deleted_at") or not server_row.get("uploaded_at"):
            return False

        files = self._client.list_files(book_hash)
        if files is None:
            return True

        cover_basename = self._client.cover_file_name(book_hash).rsplit("/", 1)[-1]
        return any(
            (f.get("file_key") or "") and not (f.get("file_key") or "").endswith(cover_basename)
            for f in files
        )

    def _publish_cover(
        self, path: Path, book_hash: str, ebook_filename: str
    ) -> tuple[Optional[str], bool]:
        """Best-effort cover extraction + upload. Never raises.

        Returns `(cover_hash, uploaded)`. Readest never converts image
        formats, so the extracted bytes are uploaded unchanged under the
        `cover.png` key even when the source image is actually a JPEG.
        """
        try:
            with tempfile.TemporaryDirectory(prefix="readest_cover_") as tmp_dir:
                cover_path = Path(tmp_dir) / "cover"
                try:
                    extracted = self._ebook_parser.extract_cover(str(path), str(cover_path))
                except Exception as e:
                    logger.debug(
                        "Readest publish: cover extraction raised for %s: %s",
                        ebook_filename, e, exc_info=True,
                    )
                    extracted = False

                if not extracted or not cover_path.is_file():
                    logger.debug("Readest publish: no cover extracted for %s", ebook_filename)
                    return None, False

                cover_bytes = cover_path.read_bytes()
                cover_hash = self._client.compute_book_hash(cover_path)
                upload_result = self._client.upload_file(
                    self._client.cover_file_name(book_hash), cover_bytes, book_hash
                )
                if not upload_result:
                    logger.warning(
                        "Readest publish: cover upload failed for %s (hash=%s)",
                        ebook_filename, book_hash,
                    )
                    return None, False
                return cover_hash, True
        except Exception as e:
            logger.warning(
                "Readest publish: cover handling failed for %s: %s",
                ebook_filename, e, exc_info=True,
            )
            return None, False

    @staticmethod
    def _is_storyteller_artifact_filename(filename: Optional[str]) -> bool:
        """Return True if `filename` is a downloaded Storyteller artifact name.

        Mirrors `web_server._is_storyteller_artifact_filename`; duplicated
        here (not imported) to avoid a circular import between the two
        modules.
        """
        return bool(filename and re.match(r"^storyteller_[0-9a-fA-F-]+\.epub$", filename))

    def publish_reading_books(self, user_id: int) -> dict[str, Any]:
        """Sweep this user's in-progress books and publish any not yet in Readest.

        Uploading the whole catalog is not acceptable against Readest's free
        tier: a measured real library carried 391 active books against a 500
        MiB quota, of which only 4 were actually in progress. So this sweep
        uploads ONLY books with a non-zero, non-complete reading position —
        never the full catalog — and skips anything whose bytes are already
        present in the user's cloud library.

        Reads its two knobs (`READEST_UPLOAD_MAX_PER_RUN`,
        `SYNC_COMPLETION_THRESHOLD`) from `os.environ` on every call, per this
        repo's settings convention. Makes at most one `pull_books` network
        call for the whole sweep (not one per candidate) since a real
        library's cloud listing can run 400+ rows.

        Defensive by design: this runs on a daemon thread, so no candidate
        failure and no unexpected exception is allowed to propagate — each
        is caught, counted as an error, and the sweep continues, except that
        a `publish_book` call which itself reports `status="error"` stops
        the whole sweep immediately (quota exhaustion/auth loss should not
        be hammered book after book).

        Returns a summary dict: `candidates`, `uploaded`, `skipped_present`,
        `errors` (all int) and `capped` (bool, True if the run stopped early
        because it hit `READEST_UPLOAD_MAX_PER_RUN`).
        """

        def _zero_summary() -> dict[str, Any]:
            return {
                "candidates": 0,
                "uploaded": 0,
                "skipped_present": 0,
                "errors": 0,
                "capped": False,
            }

        try:
            if not self._client.is_configured():
                return _zero_summary()

            if self._db is None:
                logger.warning(
                    "Readest publish_reading_books: no DatabaseService available; skipping sweep"
                )
                return _zero_summary()

            raw_cap = os.environ.get("READEST_UPLOAD_MAX_PER_RUN", "5")
            try:
                cap = int(raw_cap)
            except (TypeError, ValueError):
                cap = 5

            if cap <= 0:
                return _zero_summary()

            raw_threshold = os.environ.get("SYNC_COMPLETION_THRESHOLD", "99")
            try:
                threshold_pct = float(raw_threshold)
            except (TypeError, ValueError):
                threshold_pct = 99.0
            threshold_fraction = threshold_pct / 100.0

            books = self._db.get_books_by_status("active", user_id)
            states = self._db.get_all_states(user_id)

            best_pct_by_abs_id: dict[str, float] = {}
            for state in states:
                pct = state.percentage
                if pct is None:
                    continue
                current = best_pct_by_abs_id.get(state.abs_id)
                if current is None or pct > current:
                    best_pct_by_abs_id[state.abs_id] = pct

            candidates: list[dict[str, str]] = []
            for book in books:
                best_pct = best_pct_by_abs_id.get(book.abs_id)
                if best_pct is None:
                    continue
                if not (0 < best_pct < threshold_fraction):
                    continue

                filename = book.original_ebook_filename or book.ebook_filename
                if not filename:
                    continue
                if self._is_storyteller_artifact_filename(filename):
                    continue
                if not filename.lower().endswith(".epub"):
                    continue

                candidates.append({"abs_id": book.abs_id, "filename": filename})

            if not candidates:
                logger.info(
                    "Readest publish_reading_books: no in-progress candidates for user %s",
                    user_id,
                )
                return _zero_summary()

            rows = self._client.pull_books(since=0)
            if rows is None:
                logger.warning(
                    "Readest publish_reading_books: pull_books lookup failed for user %s; "
                    "skipping this run to avoid duplicate uploads",
                    user_id,
                )
                return _zero_summary()

            present_hashes = {
                row.get("book_hash")
                for row in rows
                if row.get("book_hash") and not row.get("deleted_at")
            }

            uploaded = 0
            skipped_present = 0
            errors = 0
            capped = False

            for candidate in candidates:
                if uploaded >= cap:
                    capped = True
                    break

                filename = candidate["filename"]
                try:
                    try:
                        resolved = self._ebook_parser.resolve_book_path(filename)
                    except FileNotFoundError:
                        logger.warning(
                            "Readest publish_reading_books: could not locate %s on disk; skipping",
                            filename, exc_info=True,
                        )
                        errors += 1
                        continue

                    book_hash = self._client.compute_book_hash(resolved)
                    if book_hash and book_hash in present_hashes:
                        skipped_present += 1
                        continue

                    result = self.publish_book(filename)
                    if result.status == "error":
                        errors += 1
                        logger.warning(
                            "Readest publish_reading_books: stopping sweep after error "
                            "publishing %s: %s",
                            filename, result.message,
                        )
                        break
                    if result.status in ("created", "updated"):
                        uploaded += 1
                    elif result.status == "skipped":
                        # `publish_book` does its own cloud-presence check, which can
                        # legitimately conclude "already there" for a book the hash
                        # pre-filter above did not catch. That is a skip, not a
                        # failure — counting it as an error makes a healthy sweep look
                        # broken in the summary log.
                        skipped_present += 1
                    else:
                        errors += 1
                except Exception as e:
                    errors += 1
                    logger.error(
                        "Readest publish_reading_books: unexpected failure processing %s: %s",
                        filename, e, exc_info=True,
                    )
                    continue

            summary = {
                "candidates": len(candidates),
                "uploaded": uploaded,
                "skipped_present": skipped_present,
                "errors": errors,
                "capped": capped,
            }
            logger.info(
                "Readest publish_reading_books: user=%s candidates=%d uploaded=%d "
                "skipped_present=%d errors=%d capped=%s",
                user_id, summary["candidates"], uploaded, skipped_present, errors, capped,
            )
            return summary
        except Exception as e:
            logger.error(
                "Readest publish_reading_books: unexpected sweep failure for user %s: %s",
                user_id, e, exc_info=True,
            )
            return _zero_summary()

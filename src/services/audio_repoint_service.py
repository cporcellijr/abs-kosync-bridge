"""Repoint a book's audiobook provider without re-matching it.

Moving a library from Audiobookshelf to BookOrbit does not change which ebook a
book is paired with — only who serves the audio. Re-matching would rebuild the
mapping from scratch under a new ``bookorbit:<id>`` primary key, orphaning every
State row, alignment map, transcript, KOSync document link, annotation and
reading session hanging off the old ``abs_id``.

None of that is necessary. ``BookOrbitAudioSyncClient._resolve_book_id`` reads
``audio_provider_book_id``/``audio_source_id`` and ``supports_book`` gates on
``audio_source``; neither touches ``abs_id``. ``ABSSyncClient.supports_book``
returns ``(audio_source or "ABS") == "ABS"``, so flipping that single field hands
the book from one provider to the other with no double-writer. The repoint is
therefore an in-place column update that preserves everything downstream.

**Duration is the safety property, not just a matching signal.** A book's
alignment map and transcript were built against the old audio. They stay valid
only if the new file is the same recording; a different narration has different
timings and would silently mis-seek every sync. Two audiobooks agreeing on
duration to within a minute are the same recording, so "the durations agree" is
simultaneously the match confirmation and the licence to keep the existing
alignment. A candidate whose duration disagrees is never applied automatically.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from src.db.models import Book
from src.utils.string_utils import calculate_similarity

logger = logging.getLogger(__name__)

# Similarity at or above which two normalized titles are considered the same book.
TITLE_MATCH_THRESHOLD = 0.92
# A duration match must be within the larger of these two — a minute of slack for
# short books, 2% for long ones (chapter padding differs between packagings).
DURATION_TOLERANCE_SECONDS = 60.0
DURATION_TOLERANCE_FRACTION = 0.02
# How many title candidates to spend a detail call on when titles tie.
MAX_CANDIDATES_TO_PROBE = 6

_NOISE_WORDS = re.compile(r"\b(unabridged|abridged|a novel|audiobook)\b", re.IGNORECASE)
_PARENTHETICAL_EDITION = re.compile(r"\((?:un)?abridged\)", re.IGNORECASE)
_LEADING_SERIES_NUMBER = re.compile(r"^\d+\s+")


class AudioRepointService:
    """Plan and apply a bulk change of audiobook provider for existing mappings."""

    def __init__(self, database_service, bookorbit_client=None) -> None:
        self.database_service = database_service
        self.bookorbit_client = bookorbit_client

    # ------------------------------------------------------------------
    # Title normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_title(title: Optional[str]) -> str:
        """Reduce a title to a comparable form.

        Strips edition noise, a leading series number (BookOrbit files audiobooks
        as "01 Sandman Slim"), and any trailing author/subtitle after " - " or ":".
        """
        text = (title or "").lower()
        text = _PARENTHETICAL_EDITION.sub(" ", text)
        text = _NOISE_WORDS.sub(" ", text)
        text = _LEADING_SERIES_NUMBER.sub("", text)
        text = text.split(" - ")[0]
        text = text.split(":")[0]
        return re.sub(r"[\W_]+", "", text)

    @staticmethod
    def durations_agree(a: Optional[float], b: Optional[float]) -> bool:
        """Whether two durations describe the same recording."""
        if not a or not b:
            return False
        try:
            a_f, b_f = float(a), float(b)
        except (TypeError, ValueError):
            return False
        tolerance = max(DURATION_TOLERANCE_SECONDS, a_f * DURATION_TOLERANCE_FRACTION)
        return abs(a_f - b_f) <= tolerance

    # ------------------------------------------------------------------
    # Candidate selection
    # ------------------------------------------------------------------

    def _repointable_books(self) -> list[Book]:
        """Active books whose audio still comes from Audiobookshelf.

        Ebook-only rows carry ``audio_source = NULL`` and are deliberately
        excluded — they have no audiobook to repoint.
        """
        out = []
        for book in self.database_service.get_all_books() or []:
            if (getattr(book, "audio_source", None) or "") != "ABS":
                continue
            if getattr(book, "status", "") != "active":
                continue
            if getattr(book, "sync_mode", "") == "ebook_only":
                continue
            out.append(book)
        return out

    def _audiobook_catalog(self) -> list[dict]:
        client = self.bookorbit_client
        if client is None or not client.is_configured():
            return []
        catalog = []
        for info in client.get_all_books() or []:
            offers = getattr(client, "_info_offers_kind", None)
            is_audio = offers(info, "audiobook") if callable(offers) else info.get("kind") == "audiobook"
            if not is_audio:
                continue
            entry = dict(info)
            entry["_norm"] = self.normalize_title(info.get("title"))
            catalog.append(entry)
        return catalog

    def _title_candidates(self, book: Book, catalog: list[dict]) -> list[tuple[float, dict]]:
        target = self.normalize_title(getattr(book, "abs_title", "") or "")
        if not target:
            return []
        scored = []
        for cand in catalog:
            norm = cand.get("_norm") or ""
            if not norm:
                continue
            # Cheap length prefilter before the O(n*m) ratio.
            if abs(len(norm) - len(target)) > max(12, len(target) * 0.5):
                continue
            score = calculate_similarity(target, norm)
            if score >= TITLE_MATCH_THRESHOLD:
                scored.append((score, cand))
        scored.sort(key=lambda item: -item[0])
        return scored

    def _candidate_duration(self, book_id) -> Optional[float]:
        client = self.bookorbit_client
        if client is None:
            return None
        try:
            info = client.get_audiobook_info(book_id) or {}
        except Exception as e:
            logger.warning(
                f"⚠️ Audio repoint: could not read BookOrbit duration for book {book_id}: {e}",
                exc_info=True,
            )
            return None
        return info.get("duration_seconds")

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------

    def build_plan(self, progress_callback: Optional[Callable[[int, int], None]] = None) -> dict[str, Any]:
        """Work out which mappings can move to BookOrbit, and which need a human.

        Returns ``{'auto': [...], 'review': [...], 'unmatched': [...], 'counts': {...}}``.
        Entries in ``auto`` are safe to apply unattended: one title match, and a
        duration that confirms the existing alignment still describes the audio.
        """
        catalog = self._audiobook_catalog()
        books = self._repointable_books()
        auto: list[dict] = []
        review: list[dict] = []
        unmatched: list[dict] = []

        if not catalog:
            logger.warning("⚠️ Audio repoint: BookOrbit returned no audiobooks; nothing to plan")
            return {
                "auto": [], "review": [], "unmatched": [],
                "counts": {"total": len(books), "auto": 0, "review": 0, "unmatched": len(books)},
            }

        total = len(books)
        for index, book in enumerate(books, start=1):
            if progress_callback:
                progress_callback(index, total)
            base = {
                "abs_id": book.abs_id,
                "title": getattr(book, "abs_title", "") or "",
                "current_source_id": getattr(book, "audio_source_id", None) or book.abs_id,
                "duration": getattr(book, "duration", None),
            }
            scored = self._title_candidates(book, catalog)
            if not scored:
                unmatched.append(dict(base, reason="no title match in BookOrbit"))
                continue

            probed = []
            for score, cand in scored[:MAX_CANDIDATES_TO_PROBE]:
                cand_duration = self._candidate_duration(cand.get("id"))
                probed.append({
                    "id": cand.get("id"),
                    "title": cand.get("title") or "",
                    "score": round(score, 4),
                    "duration": cand_duration,
                    "duration_agrees": self.durations_agree(base["duration"], cand_duration),
                })

            confirmed = [c for c in probed if c["duration_agrees"]]
            if len(confirmed) == 1:
                auto.append(dict(base, target=confirmed[0], reason="title and duration agree"))
            elif len(confirmed) > 1:
                review.append(dict(
                    base, candidates=confirmed,
                    reason="BookOrbit holds more than one copy with this title and duration",
                ))
            elif not base["duration"]:
                # No local duration to confirm with. A single title hit is still a
                # strong signal, but the alignment cannot be vouched for.
                if len(probed) == 1:
                    review.append(dict(base, candidates=probed,
                                       reason="no stored duration to confirm the recording"))
                else:
                    review.append(dict(base, candidates=probed, reason="several titles match, no duration to choose"))
            else:
                review.append(dict(
                    base, candidates=probed,
                    reason="no candidate's duration matches — likely a different narration, "
                           "which would invalidate the existing alignment",
                ))

        counts = {
            "total": total,
            "auto": len(auto),
            "review": len(review),
            "unmatched": len(unmatched),
        }
        logger.info(
            "🔁 Audio repoint plan: %d of %d can move automatically, %d need review, %d have no BookOrbit match",
            counts["auto"], counts["total"], counts["review"], counts["unmatched"],
        )
        return {"auto": auto, "review": review, "unmatched": unmatched, "counts": counts}

    # ------------------------------------------------------------------
    # Apply / undo
    # ------------------------------------------------------------------

    def _link_audio_id_for_claimants(self, abs_id: str, target_id: str) -> None:
        """Point each claimant's per-user BookOrbit link at the new audiobook.

        These books already carry a ``UserBookOrbitLink`` from their EBOOK
        mapping, with ``audio_id`` unset. ``resolve_bookorbit_audio_id`` returns
        as soon as it finds a link, so an existing ebook-only link makes it
        answer None and ``BookOrbitAudioSyncClient.supports_book`` reject the
        book — the shared ``Book.audio_source_id`` fallback is never reached.
        Updating the link is therefore part of the repoint, not an extra.
        """
        try:
            claimants = self.database_service.get_book_user_ids(abs_id) or []
        except Exception as e:
            logger.warning(
                f"⚠️ Audio repoint: could not resolve claimants for '{abs_id}': {e}",
                exc_info=True,
            )
            return
        for user_id in claimants:
            try:
                self.database_service.set_user_bookorbit_link(
                    user_id, abs_id, audio_id=str(target_id)
                )
            except Exception as e:
                logger.warning(
                    f"⚠️ Audio repoint: could not set BookOrbit audio link for user "
                    f"{user_id} on '{abs_id}': {e}",
                    exc_info=True,
                )

    def apply(self, selections: list[dict]) -> dict[str, Any]:
        """Repoint the given books. Each selection is ``{'abs_id', 'target_id'}``.

        Only ``audio_*`` columns change; ``abs_id`` is deliberately left alone so
        progress, alignments, annotations and KOSync links stay attached.
        """
        updated, skipped = 0, []
        for sel in selections or []:
            abs_id = str(sel.get("abs_id") or "").strip()
            target_id = sel.get("target_id")
            if not abs_id or target_id in (None, ""):
                skipped.append({"abs_id": abs_id, "reason": "missing abs_id or target_id"})
                continue
            book = self.database_service.get_book(abs_id)
            if book is None:
                skipped.append({"abs_id": abs_id, "reason": "book not found"})
                continue
            if (getattr(book, "audio_source", None) or "") != "ABS":
                skipped.append({"abs_id": abs_id, "reason": "audio source is no longer ABS"})
                continue

            target_id = str(target_id)
            fields = {
                "audio_source": "BookOrbit",
                "audio_source_id": target_id,
                "audio_provider_book_id": target_id,
            }
            info = None
            if self.bookorbit_client is not None:
                try:
                    info = self.bookorbit_client.get_audiobook_info(target_id)
                except Exception as e:
                    logger.warning(
                        f"⚠️ Audio repoint: could not read BookOrbit metadata for {target_id}: {e}",
                        exc_info=True,
                    )
            if info:
                if info.get("duration_seconds"):
                    fields["audio_duration"] = info.get("duration_seconds")
                if info.get("primary_file_id") is not None:
                    fields["audio_provider_file_id"] = str(info.get("primary_file_id"))

            self.database_service.update_book_fields(abs_id, **fields)
            self._link_audio_id_for_claimants(abs_id, target_id)
            updated += 1
            logger.info(
                "🔁 Audio repoint: '%s' '%s' ABS → BookOrbit book %s (progress, alignment and links kept)",
                abs_id, (getattr(book, "abs_title", "") or "")[:40], target_id,
            )

        return {"updated": updated, "skipped": skipped}

    def undo(self, abs_ids: Optional[list[str]] = None) -> dict[str, Any]:
        """Send repointed books back to Audiobookshelf.

        Recoverable without a stored backup: these rows kept their ABS item id as
        ``abs_id``, so the original ``audio_source_id`` is the primary key itself.
        """
        restored, skipped = 0, []
        if abs_ids is None:
            candidates = [
                b.abs_id for b in (self.database_service.get_all_books() or [])
                if (getattr(b, "audio_source", None) or "") == "BookOrbit"
                and not str(b.abs_id).startswith("bookorbit:")
            ]
        else:
            candidates = [str(a) for a in abs_ids]

        for abs_id in candidates:
            book = self.database_service.get_book(abs_id)
            if book is None:
                skipped.append({"abs_id": abs_id, "reason": "book not found"})
                continue
            if str(abs_id).startswith("bookorbit:"):
                skipped.append({"abs_id": abs_id, "reason": "natively BookOrbit-sourced; never came from ABS"})
                continue
            self.database_service.update_book_fields(
                abs_id,
                audio_source="ABS",
                audio_source_id=abs_id,
                audio_provider_book_id=abs_id,
            )
            restored += 1
            logger.info("↩️ Audio repoint undone: '%s' BookOrbit → ABS", abs_id)

        return {"restored": restored, "skipped": skipped}

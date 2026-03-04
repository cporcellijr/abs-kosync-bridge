from typing import Callable, List, Set, Any, Dict, Optional


class SuggestionsService:
    """Service for scanning unmatched audiobooks and producing ebook suggestions."""

    def __init__(
        self,
        database_service,
        container,
        manager,
        get_audiobooks_conditionally: Callable[[], List[dict]],
        get_searchable_ebooks: Callable[[str], List[Any]],
        audiobook_matches_search: Callable[[dict, str], bool],
        get_abs_author: Callable[[dict], str],
        logger,
    ):
        self.database_service = database_service
        self.container = container
        self.manager = manager
        self.get_audiobooks_conditionally = get_audiobooks_conditionally
        self.get_searchable_ebooks = get_searchable_ebooks
        self.audiobook_matches_search = audiobook_matches_search
        self.get_abs_author = get_abs_author
        self.logger = logger

    def get_ignored_suggestion_source_ids(self) -> Set[str]:
        """Return ABS source IDs that are marked as ignored."""
        if hasattr(self.database_service, 'get_ignored_suggestion_source_ids'):
            try:
                return set(self.database_service.get_ignored_suggestion_source_ids() or [])
            except Exception as e:
                self.logger.warning(f"Could not load ignored suggestions via service method: {e}")

        if not hasattr(self.database_service, 'get_session'):
            return set()

        from src.db.models import PendingSuggestion

        ignored_source_ids = set()
        try:
            with self.database_service.get_session() as db_session:
                rows = db_session.query(PendingSuggestion.source_id).filter(
                    PendingSuggestion.status == 'ignored'
                ).all()
                ignored_source_ids = {row[0] for row in rows if row and row[0]}
        except Exception as e:
            self.logger.warning(f"Could not load ignored suggestions: {e}")

        return ignored_source_ids

    def _scan_single_audiobook(self, ab: dict) -> Optional[dict]:
        """Scan one unmatched audiobook and return a suggestion dict or None."""
        from rapidfuzz import fuzz

        abs_id = ab.get('id')
        abs_title = (self.manager.get_abs_title(ab) or '').strip()
        abs_author = (self.get_abs_author(ab) or '').strip()
        if not abs_id or not abs_title:
            return None

        try:
            candidates = self.get_searchable_ebooks(abs_title)
        except Exception as e:
            self.logger.warning(f"Suggestion scan failed to search ebooks for '{abs_title}': {e}")
            return None

        matches = []
        for candidate in candidates:
            candidate_title = (
                getattr(candidate, 'title', None)
                or getattr(candidate, 'stem', None)
                or candidate.name
                or ''
            ).strip()
            candidate_author = (getattr(candidate, 'authors', None) or '').strip()
            if not candidate_title:
                continue

            candidate_search_text = f"{candidate_title} {candidate_author}".strip()
            direct_match = self.audiobook_matches_search(ab, candidate_search_text) if candidate_search_text else False

            title_score = float(fuzz.token_sort_ratio(abs_title, candidate_title))
            if abs_author:
                author_score = float(fuzz.token_sort_ratio(abs_author, candidate_author)) if candidate_author else 0.0
                score = (title_score * 0.7) + (author_score * 0.3)
            else:
                score = title_score

            if score < 60:
                continue

            matches.append({
                "ebook_filename": candidate.name,
                "display_name": candidate.display_name or candidate.name,
                "score": round(score, 1),
                "_direct_match": direct_match
            })

        if not matches:
            return None

        matches.sort(key=lambda m: (m.get('score', 0), 1 if m.get('_direct_match') else 0), reverse=True)
        for m in matches:
            m.pop('_direct_match', None)

        abs_client = self.container.abs_client()
        return {
            "abs_id": abs_id,
            "abs_title": abs_title,
            "abs_author": abs_author,
            "duration": self.manager.get_duration(ab),
            "cover_url": f"{abs_client.base_url}/api/items/{abs_id}/cover?token={abs_client.token}",
            "matches": matches
        }

    def scan_library_suggestions(
        self,
        cached_suggestions_by_abs: Optional[Dict[str, dict]] = None,
        cached_no_match_abs_ids: Optional[List[str]] = None,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """Scan unmatched audiobooks, reusing cache and only scanning newly unmatched IDs."""
        cached_suggestions_by_abs = dict(cached_suggestions_by_abs or {})
        cached_no_match_abs_ids_set = set(cached_no_match_abs_ids or [])

        def emit_progress(
            phase: str,
            percent: float,
            message: str,
            scanned_new_done: int,
            scanned_new_total: int,
            reused_cached: int,
            total_unmatched: int,
        ):
            if not progress_callback:
                return
            try:
                progress_callback({
                    "phase": phase,
                    "percent": int(max(0, min(100, round(percent)))),
                    "message": message,
                    "scanned_new_done": scanned_new_done,
                    "scanned_new_total": scanned_new_total,
                    "reused_cached": reused_cached,
                    "total_unmatched": total_unmatched,
                })
            except Exception:
                pass

        try:
            all_audiobooks = self.get_audiobooks_conditionally()
        except Exception as e:
            self.logger.error(f"Failed to load audiobooks for suggestions scan: {e}")
            return {
                "suggestions": [],
                "cache_by_abs": {},
                "no_match_abs_ids": [],
                "stats": {"scanned_new": 0, "reused_cached": 0, "total_unmatched": 0}
            }

        matched_abs_ids = {
            book.abs_id for book in self.database_service.get_all_books()
            if getattr(book, 'abs_id', None)
        }
        ignored_source_ids = self.get_ignored_suggestion_source_ids()

        unmatched_audiobooks = []
        for ab in all_audiobooks:
            abs_id = ab.get('id')
            if not abs_id:
                continue
            if abs_id in matched_abs_ids:
                continue
            if abs_id in ignored_source_ids:
                continue
            unmatched_audiobooks.append(ab)

        unmatched_abs_ids = {ab.get('id') for ab in unmatched_audiobooks if ab.get('id')}

        # Keep only cache entries still relevant to current unmatched universe.
        cache_by_abs = {
            abs_id: suggestion
            for abs_id, suggestion in cached_suggestions_by_abs.items()
            if abs_id in unmatched_abs_ids and abs_id not in ignored_source_ids
        }
        no_match_abs_ids_set = {
            abs_id for abs_id in cached_no_match_abs_ids_set
            if abs_id in unmatched_abs_ids and abs_id not in ignored_source_ids
        }
        reused_cached_count = len(cache_by_abs) + len(no_match_abs_ids_set)

        new_scan_candidates = [
            ab for ab in unmatched_audiobooks
            if ab.get('id') not in cache_by_abs and ab.get('id') not in no_match_abs_ids_set
        ]

        total_unmatched = len(unmatched_abs_ids)
        scanned_new_total = len(new_scan_candidates)
        scanned_new_done = 0

        if total_unmatched == 0:
            emit_progress(
                phase="finalizing",
                percent=100,
                message="No unmatched audiobooks to scan",
                scanned_new_done=0,
                scanned_new_total=0,
                reused_cached=reused_cached_count,
                total_unmatched=0,
            )
        else:
            initial_percent = (reused_cached_count / total_unmatched) * 100
            if scanned_new_total > 0:
                msg = f"Scanning 0/{scanned_new_total} new audiobooks..."
            else:
                msg = "All unmatched audiobooks served from cache"
            emit_progress(
                phase="scanning",
                percent=initial_percent,
                message=msg,
                scanned_new_done=0,
                scanned_new_total=scanned_new_total,
                reused_cached=reused_cached_count,
                total_unmatched=total_unmatched,
            )

        for idx, ab in enumerate(new_scan_candidates, start=1):
            abs_id = ab.get('id')
            suggestion = self._scan_single_audiobook(ab)
            if suggestion:
                cache_by_abs[abs_id] = suggestion
                no_match_abs_ids_set.discard(abs_id)
            else:
                no_match_abs_ids_set.add(abs_id)
            scanned_new_done = idx
            processed_total = reused_cached_count + scanned_new_done
            percent = (processed_total / total_unmatched) * 100 if total_unmatched else 100
            emit_progress(
                phase="scanning",
                percent=percent,
                message=f"Scanning {scanned_new_done}/{scanned_new_total} new audiobooks...",
                scanned_new_done=scanned_new_done,
                scanned_new_total=scanned_new_total,
                reused_cached=reused_cached_count,
                total_unmatched=total_unmatched,
            )

        suggestions = list(cache_by_abs.values())
        suggestions.sort(key=lambda s: s.get('matches', [{}])[0].get('score', 0), reverse=True)

        emit_progress(
            phase="finalizing",
            percent=100,
            message=f"Scan complete. {len(suggestions)} suggestions ready.",
            scanned_new_done=scanned_new_total,
            scanned_new_total=scanned_new_total,
            reused_cached=reused_cached_count,
            total_unmatched=total_unmatched,
        )

        return {
            "suggestions": suggestions,
            "cache_by_abs": cache_by_abs,
            "no_match_abs_ids": sorted(no_match_abs_ids_set),
            "stats": {
                "scanned_new": len(new_scan_candidates),
                "reused_cached": reused_cached_count,
                "total_unmatched": total_unmatched,
            },
        }

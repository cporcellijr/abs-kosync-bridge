"""
Regression test for the drift badge on a repointed audiobook.

When an audiobook's audio source is repointed from Audiobookshelf to BookOrbit,
the old ABS progress row is intentionally kept frozen so the repoint is undoable.
The dashboard hides the ABS tile for such books, but the drift/"Out of sync by X%"
badge was still counting that stale ABS row, producing a permanent bogus warning
that no sync can clear.

This test verifies that the stale ABS row is excluded from the drift computation
when audio_source is not 'ABS'.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import src.web_server as ws


def _mapping(states, abs_id="abs-1", duration=66944.19737, sync_mode="audiobook",
             audio_source=None, ebook_source=None):
    """Build a mapping dict for drift tests."""
    return {
        "abs_id": abs_id,
        "duration": duration,
        "sync_mode": sync_mode,
        "audio_source": audio_source,
        "ebook_source": ebook_source,
        "states": states,
    }


class TestRepointDriftExclusion(unittest.TestCase):
    """Stale ABS progress must not pollute the drift badge after repoint."""

    def setUp(self):
        ws._DASHBOARD_SYNC_WARNING_CACHE.clear()
        self.alignment = MagicMock()
        # Side effect keyed on timestamp: 36182 -> 0.5288, 46251 -> 0.6791
        def _map_time(abs_id, timestamp):
            if abs(timestamp - 36182) < 0.5:
                return 0.5288
            if abs(timestamp - 46251) < 0.5:
                return 0.6791
            return 0.5
        self.alignment.get_progress_for_time.side_effect = _map_time
        self._manager_patch = patch.object(
            ws, "manager", SimpleNamespace(alignment_service=self.alignment)
        )
        self._manager_patch.start()
        self.addCleanup(self._manager_patch.stop)
        self.addCleanup(ws._DASHBOARD_SYNC_WARNING_CACHE.clear)

    def test_stale_abs_row_excluded_returns_zero_drift(self):
        """The live report: BO Audio 69.1%, KoSync/BookOrbit 67.9%, and a frozen
        ABS row at 54.0% the card never shows. The badge read "Out of sync by
        15.0%"; with ABS excluded the surviving clients agree, so drift is 0."""
        states = {
            'abs': {'percentage': 54.05, 'timestamp': 36182},
            'bookorbitaudio': {'percentage': 69.10, 'timestamp': 46251},
            'kosync': {'percentage': 67.91, 'timestamp': 0},
            'bookorbit': {'percentage': 67.91, 'timestamp': 0},
        }
        mapping = _mapping(
            states,
            audio_source='BookOrbit',
            ebook_source='BookOrbit',
        )
        integrations = {'abs': True, 'bookorbitaudio': True, 'kosync': True, 'bookorbit': True}

        warning = ws._compute_dashboard_sync_warning_pct(mapping, integrations)

        # ABS is excluded; remaining clients (bookorbitaudio, kosync, bookorbit)
        # all read ~67.91% so drift is 0.0
        self.assertEqual(warning, 0.0)

    def test_stale_abs_row_never_converted_alignment_not_called(self):
        """The exclusion happens in the candidate list, so the stale row never
        costs an alignment-map load (issue #412's cost property still holds)."""
        states = {
            'abs': {'percentage': 54.05, 'timestamp': 36182},
            'bookorbitaudio': {'percentage': 69.10, 'timestamp': 46251},
            'kosync': {'percentage': 67.91, 'timestamp': 0},
            'bookorbit': {'percentage': 67.91, 'timestamp': 0},
        }
        mapping = _mapping(
            states,
            audio_source='BookOrbit',
            ebook_source='BookOrbit',
        )
        integrations = {'abs': True, 'bookorbitaudio': True, 'kosync': True, 'bookorbit': True}

        ws._compute_dashboard_sync_warning_pct(mapping, integrations)

        # Verify alignment service was not called with the ABS timestamp
        for call in self.alignment.get_progress_for_time.call_args_list:
            args, _ = call
            timestamp = args[1] if len(args) > 1 else None
            self.assertNotEqual(timestamp, 36182.0,
                "alignment map was called with stale ABS timestamp")

    def test_book_still_on_abs_audio_unaffected(self):
        """Genuine ABS drift is still surfaced, both for an explicit 'ABS' audio
        source and for a legacy row that leaves it unset."""
        # Test with audio_source=None (legacy, meaning ABS)
        states_legacy = {
            'abs': {'percentage': 54.05, 'timestamp': 36182},
            'kosync': {'percentage': 67.91, 'timestamp': 0},
        }
        mapping_legacy = _mapping(states_legacy, audio_source=None)
        integrations = {'abs': True, 'kosync': True}

        warning_legacy = ws._compute_dashboard_sync_warning_pct(mapping_legacy, integrations)
        # ABS maps to 52.88%, kosync reads 67.91% -> drift = 15.03 -> rounded 15.0
        self.assertEqual(warning_legacy, 15.0)

        # Reset mock call count
        self.alignment.get_progress_for_time.reset_mock()

        # Test with audio_source='ABS' (explicit)
        states_explicit = {
            'abs': {'percentage': 54.05, 'timestamp': 36182},
            'kosync': {'percentage': 67.91, 'timestamp': 0},
        }
        mapping_explicit = _mapping(states_explicit, abs_id='abs-2', audio_source='ABS')

        warning_explicit = ws._compute_dashboard_sync_warning_pct(mapping_explicit, integrations)
        self.assertEqual(warning_explicit, 15.0)


class TestGetDashboardSyncWarningClients(unittest.TestCase):
    """_get_dashboard_sync_warning_clients respects audio_source gate."""

    def test_abs_not_in_list_when_audio_source_is_bookorbit(self):
        """With audio_source='BookOrbit', 'abs' is not in the returned list
        while 'bookorbitaudio' is."""
        mapping = _mapping(
            {},
            audio_source='BookOrbit',
            ebook_source='BookOrbit',
        )
        integrations = {'abs': True, 'bookorbitaudio': True, 'kosync': True}

        clients = ws._get_dashboard_sync_warning_clients(mapping, integrations)

        self.assertNotIn('abs', clients)
        self.assertIn('bookorbitaudio', clients)
        self.assertIn('kosync', clients)

    def test_abs_in_list_when_audio_source_is_abs(self):
        """With audio_source='ABS', 'abs' is in the returned list."""
        mapping = _mapping({}, audio_source='ABS')
        integrations = {'abs': True, 'bookorbitaudio': True, 'kosync': True}

        clients = ws._get_dashboard_sync_warning_clients(mapping, integrations)

        self.assertIn('abs', clients)
        self.assertNotIn('bookorbitaudio', clients)
        self.assertIn('kosync', clients)

    def test_abs_in_list_when_audio_source_is_none_legacy(self):
        """With audio_source=None (legacy row meaning ABS), 'abs' is in the list."""
        mapping = _mapping({}, audio_source=None)
        integrations = {'abs': True, 'bookorbitaudio': True, 'kosync': True}

        clients = ws._get_dashboard_sync_warning_clients(mapping, integrations)

        self.assertIn('abs', clients)
        self.assertNotIn('bookorbitaudio', clients)
        self.assertIn('kosync', clients)


if __name__ == "__main__":
    unittest.main()
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, SyncResult, UpdateProgressRequest
from src.sync_manager import SyncManager
from src.utils.progress_metadata import extract_locator_json
from tests.base_sync_test import BaseSyncCycleTestCase


class ABSPolicySkipResultTestCase(unittest.TestCase):
    def test_backward_abs_policy_skip_is_successful_but_not_applied(self):
        abs_api = Mock()
        abs_api.get_progress.return_value = {"currentTime": 700.0}
        alignment = Mock()
        alignment.get_time_for_text.return_value = 550.0
        client = ABSSyncClient(abs_api, Mock(), Mock(), alignment_service=alignment)
        book = SimpleNamespace(
            abs_id="book-1",
            abs_title="Test Book",
            transcript_file="DB_MANAGED",
            duration=1000.0,
        )
        request = UpdateProgressRequest(
            locator_result=LocatorResult(percentage=0.55, match_index=123),
            txt="target text",
        )

        result = client.update_progress(book, request)

        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertNotIn("skipped", result.updated_state)
        self.assertEqual(result.location, 700.0)
        self.assertEqual(result.updated_state.get("ts"), 700.0)
        self.assertAlmostEqual(result.updated_state.get("pct"), 0.7)
        abs_api.update_progress.assert_not_called()

    def test_skipped_success_is_not_an_applied_write(self):
        result = SyncResult(700.0, True, {"ts": 700.0, "pct": 0.7}, skipped=True)
        self.assertFalse(SyncManager._sync_result_was_applied(result))

    def test_normal_success_is_an_applied_write(self):
        result = SyncResult(700.0, True, {"ts": 700.0, "pct": 0.7})
        self.assertTrue(SyncManager._sync_result_was_applied(result))

    def test_skipped_success_does_not_stamp_own_write_marker(self):
        manager = SyncManager.__new__(SyncManager)
        result = SyncResult(700.0, True, {"ts": 700.0, "pct": 0.7}, skipped=True)
        with patch("src.services.write_tracker.record_write") as record_write:
            manager._record_bridge_write("ABS", "book-1", result)
        record_write.assert_not_called()

    def test_normal_success_still_stamps_own_write_marker(self):
        manager = SyncManager.__new__(SyncManager)
        result = SyncResult(700.0, True, {"ts": 700.0, "pct": 0.7})
        with patch("src.services.write_tracker.record_write") as record_write:
            manager._record_bridge_write("ABS", "book-1", result)
        record_write.assert_called_once_with("ABS", "book-1", 0.7)

    def test_skipped_updated_state_serialized_without_skipped_key(self):
        """Serialization guard: the skipped marker must not leak into locator_json."""
        abs_api = Mock()
        abs_api.get_progress.return_value = {"currentTime": 700.0}
        alignment = Mock()
        alignment.get_time_for_text.return_value = 550.0
        client = ABSSyncClient(abs_api, Mock(), Mock(), alignment_service=alignment)
        book = SimpleNamespace(
            abs_id="book-1",
            abs_title="Test Book",
            transcript_file="DB_MANAGED",
            duration=1000.0,
        )
        request = UpdateProgressRequest(
            locator_result=LocatorResult(percentage=0.55, match_index=123),
            txt="target text",
        )

        result = client.update_progress(book, request)

        # A real skipped result carries only core sync values, so there is no
        # locator remainder at all. Asserted explicitly rather than guarded by
        # `if json_out is not None`, which would pass vacuously.
        self.assertIsNone(extract_locator_json(result.updated_state))

        # Belt-and-braces: a dict that DOES carry 'skipped' must have it
        # dropped while genuine locator keys survive.
        dirty = {"pct": 0.7, "xpath": "/some/path", "skipped": True}
        payload = json.loads(extract_locator_json(dirty))
        self.assertNotIn("skipped", payload)
        self.assertIn("xpath", payload)

    def test_bare_mock_is_treated_as_applied_not_skipped(self):
        """Regression guard for the Mock auto-attribute trap (CHANGE A).

        A bare unittest.mock.Mock whose 'success' is set True must be
        treated as APPLIED by _sync_result_was_applied. The old code
        (not getattr(result, 'skipped', False)) incorrectly classified a
        bare Mock as skipped because accessing 'skipped' on a Mock
        auto-creates a truthy child Mock.
        """
        bare_mock = Mock()
        bare_mock.success = True
        # 'skipped' is NOT set on the mock
        self.assertTrue(SyncManager._sync_result_was_applied(bare_mock))

    def test_propagate_completion_applied_result_persists_snapshot(self):
        """Applied completion: persists snapshot, no failure warning."""
        manager = SyncManager.__new__(SyncManager)
        manager.database_service = Mock()
        book = SimpleNamespace(abs_id="book-1", abs_title="Test Book")
        abs_id = "book-1"

        # Use a non-ABS, non-excluded client so it goes through update_progress path
        client = Mock()
        result = SyncResult(700.0, True, {"pct": 1.0})
        client.update_progress.return_value = result
        active_clients = {"BookLore": client}
        leader = "KoSync"  # Different from BookLore

        with patch.object(manager, "_persist_state_snapshot") as persist_snapshot:
            with patch("src.services.write_tracker.record_write"):
                with self.assertLogs(logger="src.sync_manager", level="INFO") as logs:
                    manager._propagate_completion(book, active_clients, leader, abs_id, "Test Book")
        
        # Snapshot should be persisted for applied result
        persist_snapshot.assert_called_once()
        # No failure warning should be logged
        warning_logs = [log for log in logs.output if "WARNING" in log]
        self.assertEqual(len(warning_logs), 0)

    def test_propagate_completion_skipped_result_persists_nothing_no_warning(self):
        """Skipped-but-successful completion: persists NOTHING, no failure warning."""
        manager = SyncManager.__new__(SyncManager)
        manager.database_service = Mock()
        book = SimpleNamespace(abs_id="book-1", abs_title="Test Book")
        abs_id = "book-1"

        # Use a non-ABS, non-excluded client so it goes through update_progress path
        client = Mock()
        result = SyncResult(700.0, True, {"pct": 1.0}, skipped=True)
        client.update_progress.return_value = result
        active_clients = {"BookLore": client}
        leader = "KoSync"  # Different from BookLore

        with patch.object(manager, "_persist_state_snapshot") as persist_snapshot:
            with patch("src.services.write_tracker.record_write"):
                with self.assertLogs(logger="src.sync_manager", level="INFO") as logs:
                    manager._propagate_completion(book, active_clients, leader, abs_id, "Test Book")
        
        # Snapshot should NOT be persisted for skipped result
        persist_snapshot.assert_not_called()
        # No failure warning should be logged
        warning_logs = [log for log in logs.output if "WARNING" in log]
        self.assertEqual(len(warning_logs), 0)
        # Should log the skip at INFO level
        info_logs = [log for log in logs.output if "INFO" in log]
        self.assertTrue(any("completion propagation skipped" in log.lower() for log in info_logs))

    def test_propagate_completion_unsuccessful_result_logs_warning(self):
        """Unsuccessful completion: logs the existing failure warning."""
        manager = SyncManager.__new__(SyncManager)
        manager.database_service = Mock()
        book = SimpleNamespace(abs_id="book-1", abs_title="Test Book")
        abs_id = "book-1"

        # Use a non-ABS, non-excluded client so it goes through update_progress path
        client = Mock()
        result = SyncResult(700.0, False, {"pct": 1.0})
        client.update_progress.return_value = result
        active_clients = {"BookLore": client}
        leader = "KoSync"  # Different from BookLore

        with patch.object(manager, "_persist_state_snapshot") as persist_snapshot:
            with patch("src.services.write_tracker.record_write"):
                with self.assertLogs(logger="src.sync_manager", level="WARNING") as logs:
                    manager._propagate_completion(book, active_clients, leader, abs_id, "Test Book")
        
        # Snapshot should NOT be persisted for failed result
        persist_snapshot.assert_not_called()
        # Failure warning should be logged
        warning_logs = [log for log in logs.output if "WARNING" in log]
        self.assertTrue(any("Completion propagation failed" in log for log in warning_logs))


class _SkippingABSSyncClient(ABSSyncClient):
    """Real ABS client whose write always reports a deliberate policy skip.

    The guard's own decision is unit-tested above against the real
    implementation; this stub exists so the cycle-level test can pin what the
    sync cycle DOES with such a result, without having to coax the alignment
    mocks into disagreeing with themselves.
    """

    SKIPPED_TS = 600.0
    SKIPPED_PCT = 0.6

    def update_progress(self, book, request) -> SyncResult:
        return SyncResult(
            self.SKIPPED_TS,
            True,
            {"ts": self.SKIPPED_TS, "pct": self.SKIPPED_PCT},
            skipped=True,
        )


class SkippedWriteStillPersistsObservedStateTestCase(BaseSyncCycleTestCase):
    """A policy skip records what was observed but claims no write.

    PR #421 gated BOTH state persistence and own-write provenance on "was this
    applied?". That conflated two different questions. The position carried by a
    skipped ABS result is a genuine fresh read from `get_progress()`, and this
    loop is the ONLY place follower state is persisted, so dropping it left the
    ABS State row stale. This test pins the split: persist the observation,
    withhold the provenance.

    Reverting the persistence gate to `_sync_result_was_applied` makes
    `test_skipped_write_still_persists_observed_state` fail.
    """

    def get_test_mapping(self):
        return {
            'abs_id': 'test-abs-id-skip',
            'abs_title': 'Policy Skip Test Book',
            'kosync_doc_id': 'test-kosync-doc-skip',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active',
            'duration': 1000.0,
        }

    def get_test_state_data(self):
        return {
            'abs': {'pct': 0.10, 'ts': 100.0, 'last_updated': 1234567890},
            'kosync': {'pct': 0.10, 'last_updated': 1234567890},
        }

    def get_expected_leader(self):
        return "KoSync"

    def get_expected_final_percentage(self):
        return 0.60

    def get_progress_mock_returns(self):
        return {
            'abs_progress': {'currentTime': 100.0, 'duration': 1000},
            'abs_in_progress': [{'id': 'test-abs-id-skip', 'progress': 0.10, 'duration': 1000}],
            'kosync_progress': (0.60, "/body/DocFragment[1]/body/p[1]"),
            'storyteller_progress': (0.0, 0.0, None, None),
            'booklore_progress': (0.0, None),
        }

    def _run_cycle(self):
        """KoSync leads at 60%; the ABS write comes back as a policy skip."""
        mocks = self.setup_common_mocks()
        mocks['ebook_parser'].resolve_xpath.return_value = "text near 60 percent"
        mocks['ebook_parser'].get_text_at_percentage.return_value = "text near 60 percent"
        mocks['ebook_parser'].find_text_location.return_value = LocatorResult(
            percentage=0.60,
            xpath="/body/DocFragment[1]/body/p[12]",
            match_index=1200,
        )
        mocks['ebook_parser'].get_perfect_ko_xpath.return_value = "/body/DocFragment[1]/body/p[12]"

        transcriber = Mock()
        transcriber.get_text_at_time.return_value = "text near 60 percent"
        transcriber.find_time_for_text.return_value = 600.0

        from src.sync_manager import SyncManager as RealSyncManager
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient

        manager = RealSyncManager(
            abs_client=mocks['abs_client'],
            booklore_client=mocks['booklore_client'],
            transcriber=transcriber,
            ebook_parser=mocks['ebook_parser'],
            database_service=mocks['database_service'],
            sync_clients={
                "ABS": _SkippingABSSyncClient(
                    mocks['abs_client'], transcriber, mocks['ebook_parser']
                ),
                "KoSync": KoSyncSyncClient(mocks['kosync_client'], mocks['ebook_parser']),
            },
            epub_cache_dir=Path(self.temp_dir) / 'epub_cache',
            data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / 'books',
        )
        manager._automatch_hardcover = Mock()
        manager._sync_to_hardcover = Mock()
        manager._get_local_epub = Mock(
            return_value=str(Path(self.temp_dir) / 'books' / 'test-book.epub')
        )

        with patch("src.services.write_tracker.record_write") as record_write:
            manager.sync_cycle()
        return mocks, record_write

    def _saved_abs_states(self, mocks):
        return [
            call.args[0]
            for call in mocks['database_service'].save_state.call_args_list
            if call.args and getattr(call.args[0], 'client_name', None) == 'abs'
        ]

    def test_skipped_write_still_persists_observed_state(self):
        mocks, _ = self._run_cycle()

        abs_states = self._saved_abs_states(mocks)
        self.assertTrue(
            abs_states,
            "A skipped write is still a fresh read of ABS; the State row must be "
            "refreshed or the dashboard goes stale",
        )
        self.assertAlmostEqual(
            abs_states[-1].percentage, _SkippingABSSyncClient.SKIPPED_PCT
        )

    def test_skipped_write_stamps_no_abs_provenance(self):
        _, record_write = self._run_cycle()

        abs_marks = [
            call for call in record_write.call_args_list
            if call.args and call.args[0] == 'ABS'
        ]
        self.assertEqual(
            abs_marks, [],
            "Nothing was written to ABS, so no own-write marker may be stamped",
        )


if __name__ == "__main__":
    unittest.main()
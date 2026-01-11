#!/usr/bin/env python3
"""
Abstract base class for sync_cycle unit tests.
Contains common setup and mock configuration to eliminate code duplication.
"""

import unittest
import os
import tempfile
from pathlib import Path
import json
from unittest.mock import Mock, patch
from abc import ABC, abstractmethod

# Import the LocatorResult class for mocking
from src.sync_clients.sync_client_interface import LocatorResult


class BaseSyncCycleTestCase(unittest.TestCase, ABC):
    """Abstract base class for sync_cycle unit tests with common mock setup."""

    def setUp(self):
        """Set up test environment and mocks - common for all sync tests."""
        # Create temporary directories for test
        self.temp_dir = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.temp_dir
        os.environ['BOOKS_DIR'] = str(Path(self.temp_dir) / 'books')
        os.environ['ABS_SERVER'] = 'http://localhost:13378'
        os.environ['ABS_TOKEN'] = 'test-token'

        # Create necessary directories
        (Path(self.temp_dir) / 'logs').mkdir(parents=True, exist_ok=True)
        (Path(self.temp_dir) / 'books').mkdir(parents=True, exist_ok=True)

        # Create dummy ebook file
        ebook_path = Path(self.temp_dir) / 'books' / 'test-book.epub'
        with open(ebook_path, 'w') as f:
            f.write("dummy epub content")

        # Get test-specific configuration from subclass
        self.test_mapping = self.get_test_mapping()
        self.test_state_data = self.get_test_state_data()
        self.expected_leader = self.get_expected_leader()
        self.expected_final_pct = self.get_expected_final_percentage()

        # Create transcript file
        transcript_data = [
            {"start": 0.0, "end": 10.0, "text": "Beginning"},
            {"start": self.expected_final_pct * 1000, "end": self.expected_final_pct * 1000 + 10,
             "text": f"{self.expected_leader} at {self.expected_final_pct * 100:.0f} percent"},
            {"start": 990.0, "end": 1000.0, "text": "End"}
        ]

        with open(self.test_mapping['transcript_file'], 'w') as f:
            json.dump(transcript_data, f)

        # Mock database data
        self.test_db_data = {'mappings': [self.test_mapping]}

    def tearDown(self):
        """Clean up after each test."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @abstractmethod
    def get_test_mapping(self):
        """Return test mapping configuration - must be implemented by subclass."""
        pass

    @abstractmethod
    def get_test_state_data(self):
        """Return test state data - must be implemented by subclass."""
        pass

    @abstractmethod
    def get_expected_leader(self):
        """Return expected leader service name - must be implemented by subclass."""
        pass

    @abstractmethod
    def get_expected_final_percentage(self):
        """Return expected final percentage (as decimal) - must be implemented by subclass."""
        pass

    @abstractmethod
    def get_progress_mock_returns(self):
        """Return progress mock return values - must be implemented by subclass."""
        pass

    def setup_common_mocks(self):
        """Set up all the common mocks used by sync tests."""
        # Create mock instances
        abs_client = Mock()
        kosync_client = Mock()
        booklore_client = Mock()
        hardcover_client = Mock()
        storyteller_db = Mock()
        ebook_parser = Mock()

        # Configure client configurations
        abs_client.is_configured.return_value = True
        kosync_client.is_configured.return_value = True
        booklore_client.is_configured.return_value = True
        hardcover_client.is_configured.return_value = False
        storyteller_db.is_configured.return_value = True

        # Get test-specific progress returns
        progress_returns = self.get_progress_mock_returns()

        # Configure progress responses
        abs_client.get_progress.return_value = progress_returns['abs_progress']
        abs_client.get_in_progress.return_value = progress_returns['abs_in_progress']
        kosync_client.get_progress.return_value = progress_returns['kosync_progress']
        storyteller_db.get_progress_with_fragment.return_value = progress_returns['storyteller_progress']
        booklore_client.get_progress.return_value = progress_returns['booklore_progress']

        # Configure update responses
        abs_client.update_progress.return_value = {"success": True}
        kosync_client.update_progress.return_value = {"success": True}
        storyteller_db.update_progress.return_value = True
        booklore_client.update_progress.return_value = True
        abs_client.create_session.return_value = f"test-session-{self.expected_leader.lower()}"

        # Configure database mocks
        db_handler = Mock()
        db_handler.load.return_value = self.test_db_data
        db_handler.save.return_value = None

        state_handler = Mock()
        state_handler.load.return_value = self.test_state_data
        state_handler.save.return_value = None

        return {
            'abs_client': abs_client,
            'kosync_client': kosync_client,
            'booklore_client': booklore_client,
            'hardcover_client': hardcover_client,
            'storyteller_db': storyteller_db,
            'ebook_parser': ebook_parser,
            'db_handler': db_handler,
            'state_handler': state_handler
        }

    def run_sync_test_with_leader_verification(self):
        """Run the sync test and verify the expected leader behavior."""

        # Set up all mocks
        mocks = self.setup_common_mocks()

        # Configure ebook parser mock
        mock_locator = LocatorResult(
            percentage=self.expected_final_pct,
            xpath=f"/html/body/div[1]/p[{int(self.expected_final_pct * 25)}]",
            match_index=int(self.expected_final_pct * 20)
        )
        mocks['ebook_parser'].find_text_location.return_value = mock_locator
        mocks['ebook_parser'].get_perfect_ko_xpath.return_value = mock_locator.xpath

        # Create transcriber mock
        transcriber = Mock()
        transcriber.get_text_at_time.return_value = f"Sample text from {self.expected_leader} leader at {self.expected_final_pct * 100:.0f}%"
        transcriber.find_time_for_text.return_value = self.expected_final_pct * 1000

        # Import SyncManager and create with dependency injection
        from main import SyncManager

        # Create sync clients with mocked dependencies
        from src.sync_clients.abs_sync_client import ABSSyncClient
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
        from src.sync_clients.booklore_sync_client import BookloreSyncClient

        abs_sync_client = ABSSyncClient(
            mocks['abs_client'],
            transcriber,
            mocks['ebook_parser'],
            mocks['db_handler']
        )
        kosync_sync_client = KoSyncSyncClient(mocks['kosync_client'], mocks['ebook_parser'])
        storyteller_sync_client = StorytellerSyncClient(mocks['storyteller_db'], mocks['ebook_parser'])
        booklore_sync_client = BookloreSyncClient(mocks['booklore_client'], mocks['ebook_parser'])

        # Create SyncManager with dependency injection (all mocks)
        manager = SyncManager(
            abs_client=mocks['abs_client'],
            kosync_client=mocks['kosync_client'],
            hardcover_client=mocks['hardcover_client'],
            storyteller_db=mocks['storyteller_db'],
            booklore_client=mocks['booklore_client'],
            transcriber=transcriber,
            ebook_parser=mocks['ebook_parser'],
            db_handler=mocks['db_handler'],
            state_handler=mocks['state_handler'],
            abs_sync_client=abs_sync_client,
            kosync_sync_client=kosync_sync_client,
            storyteller_sync_client=storyteller_sync_client,
            booklore_sync_client=booklore_sync_client,
            kosync_use_percentage_from_server=False,
            epub_cache_dir=Path(self.temp_dir) / 'epub_cache'
        )

        # Mock the ABS client's _update_abs_progress_with_offset method
        manager.abs_sync_client._update_abs_progress_with_offset = Mock(
            return_value=({"success": True}, self.expected_final_pct * 1000)
        )

        # Mock helper methods to avoid side effects
        manager._automatch_hardcover = Mock()
        manager._sync_to_hardcover = Mock()

        # Run the sync cycle
        manager.sync_cycle()

        # Perform all verifications within the same context
        self.verify_common_assertions(mocks, manager)

        # Verify final state
        final_state = self.verify_final_state(manager)

        # Return both mocks and manager for any additional verification
        return mocks, manager, final_state

    def verify_common_assertions(self, mocks, manager):
        """Verify common assertions that apply to all sync tests."""
        abs_id = self.test_mapping['abs_id']
        kosync_doc = self.test_mapping['kosync_doc_id']
        ebook_file = self.test_mapping['ebook_filename']

        # ASSERTIONS - Verify progress fetching calls
        self.assertTrue(mocks['abs_client'].get_progress.called, "ABS get_progress was not called")
        self.assertTrue(mocks['kosync_client'].get_progress.called, "KoSync get_progress was not called")
        self.assertTrue(mocks['storyteller_db'].get_progress_with_fragment.called, "Storyteller get_progress was not called")
        self.assertTrue(mocks['booklore_client'].get_progress.called, "BookLore get_progress was not called")

        leader = self.expected_leader.upper()

        if leader != "NONE":
            # Verify leader text extraction
            self.assertTrue(mocks['ebook_parser'].find_text_location.called, "EbookParser find_text_location was not called")

            # Verify update calls to followers (all non-leader services should be updated)
            if leader != 'ABS':
                # For ABS updates, check either the client update or the internal method
                abs_updated = (mocks['abs_client'].update_progress.called or
                               manager.abs_sync_client._update_abs_progress_with_offset.called)
                self.assertTrue(abs_updated, "ABS update was not called")
            if leader != 'KOSYNC':
                self.assertTrue(mocks['kosync_client'].update_progress.called, "KoSync update_progress was not called")
            if leader != 'STORYTELLER':
                self.assertTrue(mocks['storyteller_db'].update_progress.called, "Storyteller update_progress was not called")
            if leader != 'BOOKLORE':
                self.assertTrue(mocks['booklore_client'].update_progress.called, "BookLore update_progress was not called")

            # Verify state persistence
            self.assertTrue(mocks['state_handler'].save.called, "State was not saved")

        # Verify specific call arguments
        mocks['abs_client'].get_progress.assert_called_with(abs_id)
        mocks['kosync_client'].get_progress.assert_called_with(kosync_doc)
        mocks['storyteller_db'].get_progress_with_fragment.assert_called_with(ebook_file)
        mocks['booklore_client'].get_progress.assert_called_with(ebook_file)

    def verify_final_state(self, manager):
        """Verify the final state matches expected percentages."""
        abs_id = self.test_mapping['abs_id']

        # Check final state
        self.assertIn(abs_id, manager.state, f"Final state not found for {abs_id}")
        final_state = manager.state[abs_id]

        # Verify final state values
        abs_pct = final_state.get('abs_pct', 0)
        kosync_pct = final_state.get('kosync_pct', 0)
        storyteller_pct = final_state.get('storyteller_pct', 0)
        booklore_pct = final_state.get('booklore_pct', 0)

        # All services should be synced to expected percentage
        expected_pct = self.expected_final_pct
        tolerance = 0.02

        if self.get_expected_leader() != "None":
            self.assertAlmostEqual(abs_pct, expected_pct, delta=tolerance,
                                   msg=f"ABS final state {abs_pct:.1%} != expected {expected_pct:.1%}")
            self.assertAlmostEqual(kosync_pct, expected_pct, delta=tolerance,
                                   msg=f"KoSync final state {kosync_pct:.1%} != expected {expected_pct:.1%}")
            self.assertAlmostEqual(storyteller_pct, expected_pct, delta=tolerance,
                                   msg=f"Storyteller final state {storyteller_pct:.1%} != expected {expected_pct:.1%}")
            self.assertAlmostEqual(booklore_pct, expected_pct, delta=tolerance,
                                   msg=f"BookLore final state {booklore_pct:.1%} != expected {expected_pct:.1%}")

        # Verify state timestamp was updated
        self.assertIn('last_updated', final_state, "last_updated not found in final state")
        self.assertIsInstance(final_state['last_updated'], (int, float), "last_updated is not a timestamp")

        return final_state

    def run_test(self, from_percentage: float|None, target_percentage: float|None):
        """Test that the logs show the expected service correctly leading the sync."""
        import logging
        from io import StringIO

        # Capture logs to verify the expected service is detected as leader
        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        logger = logging.getLogger()
        original_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)

        try:
            # Run the sync test
            mocks, manager, final_state = self.run_sync_test_with_leader_verification()

            log_output = log_stream.getvalue()

            if from_percentage is not None and target_percentage is not None:
                # Verify the sync worked correctly
                self.verify_common_assertions(mocks, manager)
                self.verify_final_state(manager)

                # Check that the expected service was identified as leader
                self.assertIn(f"{self.get_expected_leader().upper()} leads at {target_percentage}.0000%", log_output,
                              f"Logs should show {self.get_expected_leader()} as leader")

                # Verify progress changes are logged
                self.assertIn(f"📊 {self.get_expected_leader()}: {from_percentage}.0000% -> {target_percentage}.0000%", log_output,
                              f"Logs should show {self.get_expected_leader()} progress change")

            return log_output

        finally:
            logger.removeHandler(handler)
            logger.setLevel(original_level)

#!/usr/bin/env python3
"""
Unit test for the "no changes detected" scenario using unittest.TestCase.
"""

import unittest
import sys
from pathlib import Path
# Add the project root to the path to resolve module imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.base_sync_test import BaseSyncCycleTestCase


class TestNoChangesDetectedSync(BaseSyncCycleTestCase):
    """Test case for no changes detected sync_cycle scenario."""

    def get_test_mapping(self):
        """Return no changes test mapping configuration."""
        return {
            'abs_id': 'test-abs-id-nochange',
            'abs_title': 'No Changes Test Book',
            'kosync_doc_id': 'test-kosync-doc-nochange',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active'
        }

    def get_test_state_data(self):
        """Return no changes test state data - EXACTLY matches mock returns."""
        return {
            'abs': {
                'pct': 0.15,  # 15%
                'ts': 150.0,  # timestamp
                'last_updated': 1234567890
            },
            'kosync': {
                'pct': 0.25,  # 25%
                'last_updated': 1234567890
            },
            'storyteller': {
                'pct': 0.18,  # 18%
                'last_updated': 1234567890
            },
            'booklore': {
                'pct': 0.12,  # 12%
                'last_updated': 1234567890
            }
        }

    def get_expected_leader(self):
        """Return expected leader - should be None since no changes detected."""
        return "None"

    def get_expected_final_percentage(self):
        """Return expected final percentage - should remain unchanged."""
        return 0.25  # Highest current percentage (KoSync)

    def get_progress_mock_returns(self):
        """Return progress mock return values that EXACTLY match current state."""
        return {
            'abs_progress': {'ebookProgress': 0.15, 'currentTime': 150.0, 'ebookLocation': 'epubcfi(/6/2[chapter1]!/4/2[content]/4/1:0)'},  # 15%
            'abs_in_progress': [{'id': 'test-abs-id-nochange', 'progress': 0.15, 'duration': 1000}],
            'kosync_progress': (0.25, "/html/body/div[1]/p[6]"),  # 25% (SAME as state)
            'storyteller_progress': (0.18, 18.0, "ch2", "frag2"),  # 18% (SAME as state)
            'booklore_progress': (0.12, None)  # 12% (SAME as state)
        }

    def test_no_changes_detected(self):
        """Test sync_cycle when no changes are detected (all deltas are zero)."""
        log_output = super().run_test(None, None)
        self.assertNotIn("States saved to database", log_output,
                      "Logs should show no change")


if __name__ == '__main__':
    unittest.main(verbosity=2)

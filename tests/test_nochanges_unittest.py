#!/usr/bin/env python3
"""
Unit test for the "no changes detected" scenario using unittest.TestCase.
"""

import unittest
from pathlib import Path
from base_sync_test import BaseSyncCycleTestCase


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
            'test-abs-id-nochange': {
                'abs_ts': 150.0,       # Same as mock (15%)
                'abs_pct': 0.15,       # Same as mock (15%)
                'kosync_pct': 0.25,    # Same as mock (25%)
                'storyteller_pct': 0.18,  # Same as mock (18%)
                'booklore_pct': 0.12,     # Same as mock (12%)
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
            'abs_progress': 150.0,  # 15% = 150 seconds (SAME as state)
            'abs_in_progress': [{'id': 'test-abs-id-nochange', 'progress': 0.15, 'duration': 1000}],
            'kosync_progress': (0.25, "/html/body/div[1]/p[6]"),  # 25% (SAME as state)
            'storyteller_progress': (0.18, 18.0, "ch2", "frag2"),  # 18% (SAME as state)
            'booklore_progress': (0.12, None)  # 12% (SAME as state)
        }

    def test_no_changes_detected(self):
        """Test sync_cycle when no changes are detected (all deltas are zero)."""
        log_output = super().run_test(None, None)
        self.assertNotIn("State saved to last_state.json", log_output,
                      "Logs should show no change")


if __name__ == '__main__':
    unittest.main(verbosity=2)

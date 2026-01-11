#!/usr/bin/env python3
"""
Unit test for the ABS leading scenario using unittest.TestCase.
"""

import unittest
from pathlib import Path
from base_sync_test import BaseSyncCycleTestCase

class TestABSLeadsSync(BaseSyncCycleTestCase):
    """Test case for ABS leading sync_cycle scenario."""

    def get_test_mapping(self):
        """Return ABS test mapping configuration."""
        return {
            'abs_id': 'test-abs-id-123',
            'abs_title': 'Test Audiobook',
            'kosync_doc_id': 'test-kosync-doc',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active'
        }

    def get_test_state_data(self):
        """Return ABS test state data."""
        return {
            'test-abs-id-123': {
                'abs_ts': 100.0,  # 10%
                'abs_pct': 0.1,
                'kosync_pct': 0.2,  # 20%
                'storyteller_pct': 0.1,  # 10%
                'booklore_pct': 0.0,  # 0%
                'last_updated': 1234567890
            }
        }

    def get_expected_leader(self):
        """Return expected leader service name."""
        return "ABS"

    def get_expected_final_percentage(self):
        """Return expected final percentage."""
        return 0.4  # 40%

    def get_progress_mock_returns(self):
        """Return progress mock return values for ABS leading scenario."""
        return {
            'abs_progress': 400.0,  # 40% = 400 seconds
            'abs_in_progress': [{'id': 'test-abs-id-123', 'progress': 0.4, 'duration': 1000}],
            'kosync_progress': (0.2, "/html/body/div[1]/p[5]"),  # 20%
            'storyteller_progress': (0.1, 10.0, "ch1", "frag1"),  # 10%
            'booklore_progress': (0.0, None)  # 0%
        }

    def test_abs_leads(self):
        super().run_test(10, 40)


if __name__ == '__main__':
    unittest.main(verbosity=2)

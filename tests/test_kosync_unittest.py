#!/usr/bin/env python3
"""
Unit test for the KoSync leading scenario using unittest.TestCase.
"""

import unittest
from pathlib import Path
from base_sync_test import BaseSyncCycleTestCase


class TestKoSyncLeadsSync(BaseSyncCycleTestCase):
    """Test case for KoSync leading sync_cycle scenario."""

    def get_test_mapping(self):
        """Return KoSync test mapping configuration."""
        return {
            'abs_id': 'test-abs-id-kosync',
            'abs_title': 'KoSync Leader Test Book',
            'kosync_doc_id': 'test-kosync-doc-leader',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active'
        }

    def get_test_state_data(self):
        """Return KoSync test state data."""
        return {
            'test-abs-id-kosync': {
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
        return "KoSync"

    def get_expected_final_percentage(self):
        """Return expected final percentage."""
        return 0.45  # 45%

    def get_progress_mock_returns(self):
        """Return progress mock return values for KoSync leading scenario."""
        return {
            'abs_progress': 200.0,  # 20%
            'abs_in_progress': [{'id': 'test-abs-id-kosync', 'progress': 0.2, 'duration': 1000}],
            'kosync_progress': (0.45, "/html/body/div[1]/p[15]"),  # 45% - LEADER
            'storyteller_progress': (0.15, 15.0, "ch1", "frag1"),  # 15%
            'booklore_progress': (0.1, None)  # 10%
        }

    def test_kosync_leads(self):
        super().run_test(20, 45)

if __name__ == '__main__':
    unittest.main(verbosity=2)

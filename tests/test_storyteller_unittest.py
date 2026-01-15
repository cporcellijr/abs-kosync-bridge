#!/usr/bin/env python3
"""
Unit test for the Storyteller leading scenario using unittest.TestCase.
"""

import unittest
import sys
from pathlib import Path
# Add the project root to the path to resolve module imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.base_sync_test import BaseSyncCycleTestCase


class TestStorytellerLeadsSync(BaseSyncCycleTestCase):
    """Test case for Storyteller leading sync_cycle scenario."""

    def get_test_mapping(self):
        """Return Storyteller test mapping configuration."""
        return {
            'abs_id': 'test-abs-id-storyteller',
            'abs_title': 'Storyteller Leader Test Book',
            'kosync_doc_id': 'test-kosync-doc-storyteller',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active'
        }

    def get_test_state_data(self):
        """Return Storyteller test state data."""
        return {
            'abs': {
                'pct': 0.2,  # 20%
                'ts': 200.0,  # timestamp
                'last_updated': 1234567890
            },
            'kosync': {
                'pct': 0.25,  # 25%
                'last_updated': 1234567890
            },
            'storyteller': {
                'pct': 0.3,  # 30%
                'last_updated': 1234567890
            },
            'booklore': {
                'pct': 0.1,  # 10%
                'last_updated': 1234567890
            }
        }

    def get_expected_leader(self):
        """Return expected leader service name."""
        return "Storyteller"

    def get_expected_final_percentage(self):
        """Return expected final percentage."""
        return 0.6  # 60%

    def get_progress_mock_returns(self):
        """Return progress mock return values for Storyteller leading scenario."""
        return {
            'abs_progress': {'ebookProgress': 0.3, 'currentTime': 300.0, 'ebookLocation': 'epubcfi(/6/6[chapter3]!/4/2[content]/8/1:0)'},  # 30%
            'abs_in_progress': [{'id': 'test-abs-id-storyteller', 'progress': 0.3, 'duration': 1000}],
            'kosync_progress': (0.35, "/html/body/div[1]/p[12]"),  # 35%
            'storyteller_progress': (0.6, 60.0, "ch6", "frag6"),  # 60% - LEADER
            'booklore_progress': (0.25, None)  # 25%
        }

    def test_storyteller_leads(self):
        super().run_test(30, 60)

if __name__ == '__main__':
    unittest.main(verbosity=2)

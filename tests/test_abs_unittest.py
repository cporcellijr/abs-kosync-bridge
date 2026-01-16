#!/usr/bin/env python3
"""
Unit test for the ABS leading scenario using unittest.TestCase.
"""

import unittest
import sys
from pathlib import Path
# Add the project root to the path to resolve module imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.base_sync_test import BaseSyncCycleTestCase

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
            'status': 'active',
            'duration': 1000.0  # 1000 seconds test duration
        }

    def get_test_state_data(self):
        """Return ABS test state data."""
        return {
            'abs': {
                'pct': 0.1,  # 10%
                'ts': 100.0,  # timestamp
                'last_updated': 1234567890
            },
            'kosync': {
                'pct': 0.2,  # 20%
                'last_updated': 1234567890
            },
            'storyteller': {
                'pct': 0.1,  # 10%
                'last_updated': 1234567890
            },
            'booklore': {
                'pct': 0.0,  # 0%
                'last_updated': 1234567890
            },
            'hardcover': {
                'pct': 1.0,  # 100% - highest progress but should be excluded
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
            'abs_progress': {'ebookProgress': 0.32035211267605633805, 'currentTime': 400.0, 'ebookLocation': 'epubcfi(/6/10[Afscheid_voor_even-2]!/4[Afscheid_voor_even-2]/2[book-columns]/2[book-inner]/2/230/4/2[kobo.115.2]/1:29)'},  # 40%
            'abs_in_progress': [{'id': 'test-abs-id-123', 'progress': 0.4, 'duration': 1000}],
            'kosync_progress': (0.2, "/html/body/div[1]/p[5]"),  # 20%
            'storyteller_progress': (0.1, 10.0, "ch1", "frag1"),  # 10%
            'booklore_progress': (0.0, None),  # 0%
            'hardcover_progress': (1.0, {'status_id': 2, 'page_number': 350, 'total_pages': 350})  # 100% - should be excluded
        }

    def test_abs_leads(self):
        super().run_test(10, 40)


if __name__ == '__main__':
    unittest.main(verbosity=2)

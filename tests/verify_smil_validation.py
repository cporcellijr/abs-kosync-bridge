import unittest
import sys
import os
import json
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent.parent))

# Mocking Transcriber for isolated test
class MockTranscriber:
    def validate_transcript(self, segments: list, max_overlap_ratio: float = 0.05) -> tuple[bool, float]:
        """
        Validate transcript for overlapping timestamps.
        
        Returns:
            (is_valid, overlap_ratio)
        """
        if not segments or len(segments) < 2:
            return True, 0.0
        
        overlap_count = 0
        for i in range(1, len(segments)):
            if segments[i]['start'] < segments[i-1]['end']:
                overlap_count += 1
        
        overlap_ratio = overlap_count / len(segments)
        is_valid = overlap_ratio <= max_overlap_ratio
        
        return is_valid, overlap_ratio

class TestSMILValidation(unittest.TestCase):
    def setUp(self):
        self.transcriber = MockTranscriber()

    def test_valid_transcript(self):
        # Create a valid transcript (sequential)
        segments = [
            {'start': 0.0, 'end': 10.0, 'text': 'Segment 1'},
            {'start': 10.0, 'end': 20.0, 'text': 'Segment 2'},
            {'start': 20.0, 'end': 30.0, 'text': 'Segment 3'},
            {'start': 30.0, 'end': 40.0, 'text': 'Segment 4'},
        ]
        is_valid, ratio = self.transcriber.validate_transcript(segments)
        print(f"\n[Test Valid] Ratio: {ratio:.1%}, Valid: {is_valid}")
        self.assertTrue(is_valid, "Valid transcript should pass validation")
        self.assertEqual(ratio, 0.0, "Valid transcript should have 0 overlap")

    def test_minor_overlap_transcript(self):
        # Create a transcript with minor overlap (acceptable < 5%)
        # 10 segments, 1 overlap (10% would fail.. wait 5% threshold)
        # Let's do 20 segments, 1 overlap = 5% -> borderline
        # Let's do 25 segments, 1 overlap = 4% -> pass
        segments = []
        for i in range(25):
            start = i * 10
            end = start + 10
            # Force overlap on 2nd segment
            if i == 1:
                start = 5 # Overlaps with 0-10
            
            segments.append({'start': start, 'end': end, 'text': f'Segment {i}'})

        is_valid, ratio = self.transcriber.validate_transcript(segments)
        print(f"[Test Minor Overlap] Ratio: {ratio:.1%}, Valid: {is_valid}")
        self.assertTrue(is_valid, f"Minor overlap ({ratio:.1%}) should pass validation (Threshold 5%)")

    def test_corrupt_transcript(self):
        # Create a corrupt transcript (high overlap)
        # Scenario: All segments start at 0 (common corruption)
        segments = [
            {'start': 0.0, 'end': 10.0, 'text': 'Segment 1'},
            {'start': 0.0, 'end': 10.0, 'text': 'Segment 2'},
            {'start': 0.0, 'end': 10.0, 'text': 'Segment 3'},
            {'start': 0.0, 'end': 10.0, 'text': 'Segment 4'},
        ]
        is_valid, ratio = self.transcriber.validate_transcript(segments)
        print(f"[Test Corrupt] Ratio: {ratio:.1%}, Valid: {is_valid}")
        self.assertFalse(is_valid, "Corrupt transcript should fail validation")
        self.assertGreater(ratio, 0.5, "Corrupt transcript should have high overlap")

    def test_interleaved_transcript(self):
        # Scenario: Interleaved chapters causing cascade overlaps
        # Seg 1: 0-10
        # Seg 2: 5-15 (Overlap)
        # Seg 3: 10-20 (Overlap with 2)
        # Seg 4: 15-25 (Overlap with 3)
        segments = [
            {'start': 0.0, 'end': 10.0},
            {'start': 5.0, 'end': 15.0},
            {'start': 10.0, 'end': 20.0},
            {'start': 15.0, 'end': 25.0},
        ]
        is_valid, ratio = self.transcriber.validate_transcript(segments)
        print(f"[Test Interleaved] Ratio: {ratio:.1%}, Valid: {is_valid}")
        # 3 overlaps out of 4 segments = 75%
        self.assertFalse(is_valid, "Interleaved transcript should fail validation")

if __name__ == '__main__':
    unittest.main()

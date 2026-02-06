import pytest
import json
from unittest.mock import MagicMock
from src.services.alignment_service import AlignmentService
from src.utils.polisher import Polisher
from src.db.models import BookAlignment

@pytest.fixture
def mock_db():
    db = MagicMock()
    session = MagicMock()
    db.get_session.return_value = session
    return db

@pytest.fixture
def service(mock_db):
    return AlignmentService(mock_db, Polisher())

def test_align_and_store_success(service, mock_db):
    ebook_text = "Alice in Wonderland"
    segments = [{'start': 0.0, 'end': 1.0, 'text': "Alice"}]
    
    # Setup Session Context
    session = mock_db.get_session()
    session.__enter__.return_value = session
    
    # Mock lower-level alignment logic (tested separately in test_generate_alignment_map)
    # We only want to verify the storage flow here
    service._generate_alignment_map = MagicMock(return_value=[{'char': 0, 'ts': 0.0}, {'char': 5, 'ts': 1.0}])
    
    # Ensure DB query returns None (Simulate no existing record)
    session.query.return_value.filter_by.return_value.first.return_value = None
    
    result = service.align_and_store("test_id", segments, ebook_text)
    
    assert result == True
    session.add.assert_called()

def test_generate_alignment_map(service):
    ebook_text = "One two three four five."
    segments = [
        {'start': 0.0, 'end': 1.0, 'text': "One two"},
        {'start': 1.0, 'end': 2.0, 'text': "three four"},
        {'start': 2.0, 'end': 3.0, 'text': "five"}
    ]
    
    # N=12 in implementation is large, so with short text it might fail finding anchors?
    # Actually, N=12 refers to N-grams of WORDS? 
    # Code: keys = [x['word'] for x in items[i:i+N]] -> Yes, 12 words.
    # So short text won't align with N=12.
    # We need longer text for this test or need to mock the constant.
    
    # Let's mock the N constant or provide long text?
    # Providing long text is safer.
    
    tokens = ["word" + str(i) for i in range(20)]
    ebook_text = " ".join(tokens)
    
    # Create segments roughly matching
    segments = []
    for i in range(20):
        segments.append({'start': float(i), 'end': float(i+1), 'text': tokens[i]})
        
    alignment_map = service._generate_alignment_map(segments, ebook_text)
    
    assert len(alignment_map) > 0
    # Should contain start (0,0) and likely some anchors
    assert alignment_map[0]['char'] == 0
    assert alignment_map[0]['ts'] == 0.0

def test_get_time_for_text(service, mock_db):
    # Mock _get_alignment return
    mock_map = [
        {'char': 0, 'ts': 0.0},
        {'char': 100, 'ts': 10.0}
    ]
    
    session = mock_db.get_session()
    session.__enter__.return_value = session
    mock_entry = MagicMock()
    mock_entry.alignment_map_json = json.dumps(mock_map)
    session.query.return_value.filter_by.return_value.first.return_value = mock_entry
    
    # Test Exact
    ts = service.get_time_for_text("test_id", "query", char_offset_hint=0)
    assert ts == 0.0
    
    # Test Interpolation (50 chars -> 5.0s)
    ts = service.get_time_for_text("test_id", "query", char_offset_hint=50)
    assert ts == 5.0

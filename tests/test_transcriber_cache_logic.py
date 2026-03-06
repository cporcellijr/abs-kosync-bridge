import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.utils.transcriber import AudioTranscriber


def test_stalign_command_defaults_include_sentence_granularity():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        transcriber = AudioTranscriber(root, MagicMock(), MagicMock())
        cmd = transcriber._build_stalign_command(root / "book.epub", root / "audio", root / "out")
        assert "--granularity" in cmd
        assert cmd[cmd.index("--granularity") + 1] == "sentence"
        assert "--no-progress" in cmd

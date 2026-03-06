import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.utils.transcriber import AudioTranscriber


def _make_transcriber(tmp_path: Path) -> AudioTranscriber:
    return AudioTranscriber(tmp_path, MagicMock(), MagicMock())


def test_transcribe_with_stalign_success_creates_cached_readaloud():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        transcriber = _make_transcriber(root)
        transcriber.stalign_path = "/bin/echo"

        epub = root / "book.epub"
        epub.write_text("epub")
        audio_dir = root / "audio"
        audio_dir.mkdir(parents=True)

        def _run(cmd, capture_output, text, timeout):
            out_dir = Path(cmd[cmd.index("--output") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "book_readaloud.epub").write_text("readaloud")
            return MagicMock(returncode=0, stdout="ok", stderr="")

        with patch("src.utils.transcriber.subprocess.run", side_effect=_run):
            out_path = transcriber.transcribe_with_stalign("abs-1", epub, audio_dir)

        assert out_path is not None
        assert out_path.name == "abs-1_readaloud.epub"
        assert out_path.exists()


def test_transcribe_with_stalign_returns_none_on_failure():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        transcriber = _make_transcriber(root)
        transcriber.stalign_path = "/bin/echo"

        epub = root / "book.epub"
        epub.write_text("epub")
        audio_dir = root / "audio"
        audio_dir.mkdir(parents=True)

        with patch("src.utils.transcriber.subprocess.run", return_value=MagicMock(returncode=2, stdout="", stderr="bad")):
            out_path = transcriber.transcribe_with_stalign("abs-2", epub, audio_dir)

        assert out_path is None


def test_transcribe_with_stalign_returns_none_on_timeout():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        transcriber = _make_transcriber(root)
        transcriber.stalign_path = "/bin/echo"

        epub = root / "book.epub"
        epub.write_text("epub")
        audio_dir = root / "audio"
        audio_dir.mkdir(parents=True)

        with patch(
            "src.utils.transcriber.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="stalign", timeout=60),
        ):
            out_path = transcriber.transcribe_with_stalign("abs-3", epub, audio_dir)

        assert out_path is None


def test_build_stalign_cli_for_all_engines_and_sanitizes_keys():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        transcriber = _make_transcriber(root)
        transcriber.stalign_path = "/usr/local/bin/stalign"

        epub = root / "book.epub"
        audio = root / "audio"
        out = root / "out"

        engine_expectations = {
            "whisper.cpp": "--model",
            "openai-cloud": "--openai-api-key",
            "deepgram": "--deepgram-api-key",
            "whisper-server": "--whisper-server-url",
            "google-cloud": "--google-cloud-project",
            "microsoft-azure": "--azure-speech-key",
            "amazon-transcribe": "--aws-access-key-id",
        }

        env_payload = {
            "STALIGN_WHISPER_MODEL": "tiny.en",
            "STALIGN_OPENAI_API_KEY": "openai-secret",
            "STALIGN_OPENAI_BASE_URL": "http://openai.local/v1",
            "STALIGN_OPENAI_MODEL": "gpt-4o-mini-transcribe",
            "STALIGN_DEEPGRAM_API_KEY": "dg-secret",
            "STALIGN_DEEPGRAM_MODEL": "nova-3",
            "STALIGN_WHISPER_SERVER_URL": "http://whisper.local",
            "STALIGN_WHISPER_SERVER_API_KEY": "ws-secret",
            "STALIGN_GOOGLE_CLOUD_PROJECT": "proj",
            "STALIGN_GOOGLE_CLOUD_LOCATION": "us",
            "STALIGN_GOOGLE_CLOUD_LANGUAGE": "en-US",
            "STALIGN_AZURE_SPEECH_KEY": "az-secret",
            "STALIGN_AZURE_SPEECH_REGION": "eastus",
            "STALIGN_AZURE_LANGUAGE": "en-US",
            "STALIGN_AWS_ACCESS_KEY_ID": "aws-id",
            "STALIGN_AWS_SECRET_ACCESS_KEY": "aws-secret",
            "STALIGN_AWS_REGION": "us-east-1",
            "STALIGN_AWS_LANGUAGE": "en-US",
        }

        with patch.dict(os.environ, env_payload, clear=False):
            for engine, required_flag in engine_expectations.items():
                cmd = transcriber._build_stalign_command(
                    epub_path=epub,
                    audiobook_dir=audio,
                    output_dir=out,
                    engine_config={"STALIGN_ENGINE": engine},
                )
                assert "--engine" in cmd
                assert required_flag in cmd
                assert "--no-progress" in cmd
                assert "--granularity" in cmd

        deepgram_cmd = transcriber._build_stalign_command(
            epub_path=epub,
            audiobook_dir=audio,
            output_dir=out,
            engine_config={"STALIGN_ENGINE": "deepgram", "STALIGN_DEEPGRAM_API_KEY": "secret123"},
        )
        sanitized = transcriber._sanitize_stalign_command(deepgram_cmd)
        assert "secret123" not in " ".join(sanitized)

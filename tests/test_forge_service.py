import unittest
from unittest.mock import MagicMock, patch, ANY
import json
import os
import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.forge_service import ForgeService


class TestForgeService(unittest.TestCase):
    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_abs = MagicMock()
        self.mock_booklore = MagicMock()
        self.mock_storyteller = MagicMock()
        self.mock_library = MagicMock()
        self.mock_cwa = MagicMock()
        self.mock_kavita = MagicMock()
        self.mock_library.cwa_client = self.mock_cwa
        self.mock_library.kavita_client = self.mock_kavita
        self.mock_ebook_parser = MagicMock()
        self.mock_transcriber = MagicMock()
        self.mock_alignment = MagicMock()
        
        self.service = ForgeService(
            database_service=self.mock_db,
            abs_client=self.mock_abs,
            booklore_client=self.mock_booklore,
            storyteller_client=self.mock_storyteller,
            library_service=self.mock_library,
            ebook_parser=self.mock_ebook_parser,
            transcriber=self.mock_transcriber,
            alignment_service=self.mock_alignment
        )
        
        # Suppress logging during tests
        self.logger_patch = patch('src.services.forge_service.logger')
        self.logger_patch.start()

    def tearDown(self):
        patch.stopall()

    def test_start_manual_forge(self):
        """Test starting a manual forge process."""
        # We process start_manual_forge which creates a thread targeting _forge_background_task
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread_instance = MagicMock()
            mock_thread_cls.return_value = mock_thread_instance
            
            self.service.start_manual_forge(
                abs_id="abs456",
                text_item={"path": "other.epub"},
                title="Test Book 2",
                author="Test Author 2"
            )
            
            mock_thread_cls.assert_called_with(
                target=self.service._forge_background_task,
                args=("abs456", {"path": "other.epub"}, "Test Book 2", "Test Author 2"),
                daemon=True
            )
            mock_thread_instance.start.assert_called_once()

    def test_start_manual_forge_hardlink_passes_stage_mode_kwargs(self):
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread_instance = MagicMock()
            mock_thread_cls.return_value = mock_thread_instance

            self.service.start_manual_forge(
                abs_id="abs456",
                text_item={"path": "other.epub"},
                title="Test Book 2",
                author="Test Author 2",
                stage_mode="hardlink",
            )

            mock_thread_cls.assert_called_with(
                target=self.service._forge_background_task,
                args=("abs456", {"path": "other.epub"}, "Test Book 2", "Test Author 2"),
                kwargs={"stage_mode": "hardlink"},
                daemon=True
            )
            mock_thread_instance.start.assert_called_once()

    def test_start_auto_forge_match(self):
        """Test starting auto forge match."""
        # Using mock threading
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread_instance = MagicMock()
            mock_thread_cls.return_value = mock_thread_instance
            
            self.service.start_auto_forge_match(
                abs_id="abs789",
                text_item={"booklore_id": 1},
                title="Auto Book",
                author="Auto Author",
                original_filename="orig.epub",
                original_hash="hash123"
            )
            
            mock_thread_cls.assert_called_with(
                target=self.service._auto_forge_background_task,
                args=("abs789", {"booklore_id": 1}, "Auto Book", "Auto Author", "orig.epub", "hash123",
                      None, None),
                daemon=True
            )
            mock_thread_instance.start.assert_called_once()

    def test_start_auto_forge_match_hardlink_passes_stage_mode_kwargs(self):
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread_instance = MagicMock()
            mock_thread_cls.return_value = mock_thread_instance

            self.service.start_auto_forge_match(
                abs_id="abs789",
                text_item={"booklore_id": 1},
                title="Auto Book",
                author="Auto Author",
                original_filename="orig.epub",
                original_hash="hash123",
                stage_mode="hardlink",
            )

            mock_thread_cls.assert_called_with(
                target=self.service._auto_forge_background_task,
                args=("abs789", {"booklore_id": 1}, "Auto Book", "Auto Author", "orig.epub", "hash123",
                      None, None),
                kwargs={"stage_mode": "hardlink"},
                daemon=True
            )
            mock_thread_instance.start.assert_called_once()

    def test_stage_local_file_prefers_hardlink_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "source.epub"
            dest = tmp_path / "dest.epub"
            source.write_bytes(b"source")

            with patch("src.services.forge_service.os.link") as mock_link, patch(
                "src.services.forge_service.shutil.copy2"
            ) as mock_copy:
                result = self.service._stage_local_file(source, dest, "hardlink", "Forge")

            self.assertEqual(result, "hardlink")
            mock_link.assert_called_once_with(source, dest)
            mock_copy.assert_not_called()

    def test_stage_local_file_hardlink_falls_back_to_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "source.epub"
            dest = tmp_path / "dest.epub"
            source.write_bytes(b"source")

            with patch("src.services.forge_service.os.link", side_effect=OSError("no hardlink")), patch(
                "src.services.forge_service.shutil.copy2"
            ) as mock_copy:
                result = self.service._stage_local_file(source, dest, "hardlink", "Forge")

            self.assertEqual(result, "copy")
            mock_copy.assert_called_once_with(str(source), dest)

    def test_discover_storyteller_uuid_ignores_fuzzy_non_exact_title_match(self):
        """Forge UUID discovery should not accept unrelated fuzzy Storyteller search hits."""
        st_client = MagicMock()
        st_client.find_book_by_staged_path.return_value = None
        st_client.search_books.return_value = [
            {"uuid": "wrong-uuid", "title": "The Last Murder at the End of the World"}
        ]

        result = self.service._discover_storyteller_uuid(
            st_client=st_client,
            safe_title="Last Days",
            epub_filename="Last Days.epub",
            title="Last Days",
        )

        self.assertIsNone(result)

    def test_discover_storyteller_uuid_accepts_normalized_exact_title_match(self):
        """Forge UUID discovery should allow normalized exact matches without fuzzy fallback."""
        st_client = MagicMock()
        st_client.find_book_by_staged_path.return_value = None
        st_client.search_books.return_value = [
            {"uuid": "uuid-1", "title": "Last Days"},
            {"uuid": "wrong-uuid", "title": "The Last Murder at the End of the World"},
        ]

        result = self.service._discover_storyteller_uuid(
            st_client=st_client,
            safe_title="Last Days",
            epub_filename="Last Days.epub",
            title="Last Days",
        )

        self.assertEqual(result, "uuid-1")

    def _write_storyteller_manifest(self, base_dir: Path) -> str:
        manifest_dir = base_dir / "storyteller_manifest"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        chapter_file = manifest_dir / "00000-00001.json"
        chapter_payload = {
            "transcript": "hello world",
            "wordTimeline": [
                {
                    "startTime": 0.0,
                    "endTime": 0.5,
                    "startOffsetUtf16": 0,
                    "endOffsetUtf16": 5
                }
            ]
        }
        chapter_file.write_text(json.dumps(chapter_payload), encoding="utf-8")
        manifest = {
            "format": "storyteller_manifest",
            "duration": 10.0,
            "chapters": [
                {
                    "index": 0,
                    "file": chapter_file.name,
                    "start": 0.0,
                    "end": 10.0
                }
            ]
        }
        manifest_path = manifest_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return str(manifest_path)

    def _run_auto_forge_pipeline(
        self,
        text_item: dict,
        stage_mode: str = "cleanup",
        ingest_manifest: str = None,
        storyteller_alignment_ok: bool = False,
        smil_transcript=None,
        whisper_transcript=None,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            storyteller_library = tmp_path / "storyteller_library"
            epub_cache_dir = tmp_path / "epub_cache"

            title = "Auto Book"
            final_course_dir = storyteller_library / title
            final_course_dir.mkdir(parents=True, exist_ok=True)
            (final_course_dir / f"{title}_readaloud.epub").write_bytes(b"readaloud")

            source_epub = tmp_path / "source.epub"
            source_epub.write_bytes(b"source")
            if text_item.get("source") == "Local File" and not text_item.get("path"):
                text_item["path"] = str(source_epub)

            def _copy_audio(_abs_id, dest_path, stage_mode="cleanup"):
                dest = Path(dest_path)
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "part_001.mp3").write_bytes(b"audio")
                return True

            self.service._copy_audio_files = MagicMock(side_effect=_copy_audio)
            self.mock_ebook_parser.epub_cache_dir = epub_cache_dir
            self.mock_ebook_parser.extract_text_and_map.return_value = ("full text", {})
            self.mock_alignment.align_storyteller_and_store.return_value = storyteller_alignment_ok
            self.mock_alignment.align_and_store.return_value = True
            if smil_transcript is None:
                self.mock_transcriber.transcribe_from_smil.return_value = [{"ts": 0.0, "char": 0}]
            else:
                self.mock_transcriber.transcribe_from_smil.return_value = smil_transcript
            self.mock_transcriber.process_audio.return_value = whisper_transcript

            self.mock_abs.get_item_details.return_value = {
                "media": {"chapters": [{"start": 0.0, "end": 5.0}]}
            }
            self.mock_abs.get_audio_files.return_value = [{"stream_url": "http://audio.test/1.mp3", "ext": "mp3"}]
            self.mock_abs.add_to_collection.return_value = True
            self.mock_booklore.add_to_shelf.return_value = True

            self.mock_storyteller.find_book_by_staged_path.return_value = "uuid-1"
            self.mock_storyteller.search_books.return_value = []
            self.mock_storyteller.trigger_processing.return_value = True
            self.mock_storyteller.get_book_details.return_value = {
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0},
                "audiobook": {"filepath": "/storyteller/library/Auto Book", "missing": 0},
            }
            self.mock_storyteller.add_to_collection_by_uuid.return_value = True
            self.mock_storyteller.add_to_collection.return_value = True

            def _download_storyteller_book(_uuid, output_path):
                output = Path(output_path)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"artifact")
                return True

            self.mock_storyteller.download_book.side_effect = _download_storyteller_book

            db_book = MagicMock()
            self.mock_db.get_book.return_value = db_book
            self.mock_db.save_book.return_value = db_book

            with patch.dict(
                os.environ,
                {
                    "STORYTELLER_LIBRARY_DIR": str(storyteller_library),
                    "ABS_COLLECTION_NAME": "Synced with KOReader",
                    "BOOKLORE_SHELF_NAME": "Kobo",
                },
                clear=False,
            ), patch("src.services.forge_service.time.sleep", return_value=None), patch(
                "src.services.forge_service.ingest_storyteller_transcripts",
                return_value=ingest_manifest,
            ):
                self.service._auto_forge_background_task(
                    abs_id="abs-1",
                    text_item=text_item,
                    title=title,
                    author="Auto Author",
                    original_filename="orig.epub",
                    original_hash="hash123",
                    stage_mode=stage_mode,
                )

            return db_book

    def test_auto_forge_cwa_falls_back_to_cwa_id_lookup(self):
        """Auto-forge should use CWA ID lookup when no direct download URL is provided."""
        def _download_cwa(url, output_path):
            Path(output_path).write_bytes(b"source")
            return True

        self.mock_cwa.download_ebook.side_effect = _download_cwa
        self.mock_cwa.get_book_by_id.return_value = {"download_url": "http://example.test/book.epub"}

        self._run_auto_forge_pipeline(
            text_item={"source": "CWA", "cwa_id": "123", "download_url": ""},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
        )

        self.mock_cwa.get_book_by_id.assert_called_once_with("123")
        self.mock_cwa.download_ebook.assert_any_call("http://example.test/book.epub", ANY)

    def test_auto_forge_kavita_falls_back_to_kavita_id_lookup(self):
        """Auto-forge should use Kavita ID lookup when no direct download URL is provided."""
        self.mock_kavita.download_book.return_value = b"x" * 2048
        self.mock_kavita.add_to_want_to_read.return_value = True

        self._run_auto_forge_pipeline(
            text_item={"source": "Kavita", "kavita_id": "321", "kavita_series_id": "99", "download_url": ""},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
        )

        self.mock_kavita.download_book.assert_called_with("321")
        self.mock_kavita.add_to_want_to_read.assert_called_with("99")

    def test_auto_forge_uses_storyteller_uuid_collection_path(self):
        """Auto-forge should add Storyteller books to collection by UUID when available."""
        self._run_auto_forge_pipeline(
            text_item={"source": "Local File"},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
        )

        self.mock_storyteller.add_to_collection_by_uuid.assert_called_once_with("uuid-1")
        self.mock_storyteller.add_to_collection.assert_not_called()

    def test_auto_forge_uses_storyteller_alignment_before_smil(self):
        """Storyteller transcript alignment should run first; SMIL is fallback only."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._write_storyteller_manifest(Path(tmp))
            self._run_auto_forge_pipeline(
                text_item={"source": "Local File"},
                ingest_manifest=manifest_path,
                storyteller_alignment_ok=True,
            )

        self.mock_alignment.align_storyteller_and_store.assert_called_once()
        self.mock_transcriber.transcribe_from_smil.assert_not_called()
        self.mock_alignment.align_and_store.assert_not_called()

    def test_auto_forge_falls_back_to_whisper_when_smil_rejected(self):
        """Auto-forge should run Whisper fallback if SMIL returns no transcript."""
        self._run_auto_forge_pipeline(
            text_item={"source": "Local File"},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
            smil_transcript=[],
            whisper_transcript=[{"start": 0.0, "end": 1.0, "text": "hello"}],
        )

        self.mock_abs.get_audio_files.assert_called_once_with("abs-1")
        self.mock_transcriber.process_audio.assert_called_once()
        self.mock_alignment.align_and_store.assert_called_once()

    def test_auto_forge_uses_smil_and_skips_whisper_when_smil_available(self):
        """Auto-forge should not call Whisper when SMIL transcript is valid."""
        self._run_auto_forge_pipeline(
            text_item={"source": "Local File"},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
            smil_transcript=[{"start": 0.0, "end": 1.0, "text": "from smil"}],
            whisper_transcript=[{"start": 0.0, "end": 1.0, "text": "from whisper"}],
        )

        self.mock_transcriber.process_audio.assert_not_called()
        self.mock_alignment.align_and_store.assert_called_once()

    def test_auto_forge_runs_final_cleanup_on_pipeline_failure(self):
        """Auto-forge should still try source cleanup after post-download pipeline failures."""
        with patch.object(self.service, "_cleanup_staged_sources", return_value=0) as mock_cleanup:
            db_book = self._run_auto_forge_pipeline(
                text_item={"source": "Local File"},
                ingest_manifest=None,
                storyteller_alignment_ok=False,
                smil_transcript=[],
                whisper_transcript=[],
            )

        mock_cleanup.assert_called_once()
        self.assertEqual(db_book.status, "error")

    def test_auto_forge_hardlink_mode_skips_final_cleanup(self):
        with patch.object(self.service, "_cleanup_staged_sources", return_value=0) as mock_cleanup:
            self._run_auto_forge_pipeline(
                text_item={"source": "Local File"},
                stage_mode="hardlink",
                ingest_manifest=None,
                storyteller_alignment_ok=False,
            )

        mock_cleanup.assert_not_called()

    def test_poll_auto_forge_completion_api_metadata_does_not_mark_complete(self):
        """Metadata readiness alone should not mark auto-forge complete."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            course_dir = tmp_path / "course"
            course_dir.mkdir(parents=True, exist_ok=True)
            epub_cache = tmp_path / "epub_cache"
            epub_cache.mkdir(parents=True, exist_ok=True)

            st_client = MagicMock()
            st_client.get_book_details.return_value = {
                "readaloud": {"filepath": "/storyteller/output/readaloud.epub"}
            }
            st_client.download_book.return_value = False

            with patch.object(self.service, "_find_processed_epub", return_value=None):
                result = self.service._poll_auto_forge_completion(
                    st_client=st_client,
                    safe_title="Auto Book",
                    epub_filename="Auto Book.epub",
                    title="Auto Book",
                    course_dir=course_dir,
                    epub_cache=epub_cache,
                    found_uuid="uuid-1",
                    processing_triggered=True,
                    poll_count=1,
                )

            self.assertTrue(result["api_ready_seen"])
            self.assertIsNone(result["completion_method"])

    def test_poll_auto_forge_completion_does_not_trigger_before_processing_ready(self):
        """Auto-forge should delay /process until Storyteller exposes linked ebook and audiobook."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            course_dir = tmp_path / "course"
            course_dir.mkdir(parents=True, exist_ok=True)
            epub_cache = tmp_path / "epub_cache"
            epub_cache.mkdir(parents=True, exist_ok=True)

            st_client = MagicMock()
            st_client.get_book_details.return_value = {
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0}
            }
            st_client.download_book.return_value = False

            with patch.object(self.service, "_find_processed_epub", return_value=None):
                result = self.service._poll_auto_forge_completion(
                    st_client=st_client,
                    safe_title="Auto Book",
                    epub_filename="Auto Book.epub",
                    title="Auto Book",
                    course_dir=course_dir,
                    epub_cache=epub_cache,
                    found_uuid="uuid-1",
                    processing_triggered=False,
                    poll_count=4,
                )

            st_client.trigger_processing.assert_not_called()
            self.assertFalse(result["processing_triggered"])

    def test_poll_auto_forge_completion_triggers_when_processing_ready(self):
        """Auto-forge should trigger processing once Storyteller reports both links ready."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            course_dir = tmp_path / "course"
            course_dir.mkdir(parents=True, exist_ok=True)
            epub_cache = tmp_path / "epub_cache"
            epub_cache.mkdir(parents=True, exist_ok=True)

            st_client = MagicMock()
            st_client.get_book_details.return_value = {
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0},
                "audiobook": {"filepath": "/storyteller/library/Auto Book", "missing": 0},
            }
            st_client.download_book.return_value = False

            with patch.object(self.service, "_find_processed_epub", return_value=None):
                result = self.service._poll_auto_forge_completion(
                    st_client=st_client,
                    safe_title="Auto Book",
                    epub_filename="Auto Book.epub",
                    title="Auto Book",
                    course_dir=course_dir,
                    epub_cache=epub_cache,
                    found_uuid="uuid-1",
                    processing_triggered=False,
                    poll_count=4,
                )

            st_client.trigger_processing.assert_called_once_with("uuid-1")
            self.assertTrue(result["processing_triggered"])

    def test_auto_forge_waits_for_storyteller_processing_readiness_before_trigger(self):
        """Auto-forge should poll readiness instead of triggering immediately on UUID discovery."""
        responses = [
            None,
            {
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0}
            },
            {
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0},
                "audiobook": {"filepath": "/storyteller/library/Auto Book", "missing": 0},
            },
        ]

        def _next_details(*_args, **_kwargs):
            if len(responses) > 1:
                return responses.pop(0)
            return responses[0]

        self.mock_storyteller.get_book_details.side_effect = _next_details

        self._run_auto_forge_pipeline(
            text_item={"source": "Local File"},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
        )

        self.assertGreaterEqual(self.mock_storyteller.get_book_details.call_count, 3)
        self.mock_storyteller.trigger_processing.assert_called_once_with("uuid-1")


if __name__ == '__main__':
    unittest.main()

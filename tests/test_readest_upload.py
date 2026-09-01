"""
Tests for the Readest cloud publish path:
  - ReadestClient.compute_meta_hash (live-validated golden vector)
  - ReadestUploadService.publish_book (progress carry-over, group handling,
    cloud-presence detection, upload failure, early-outs, DC metadata
    derivation)
  - web_server._publish_saved_ebook_to_readest (the READEST_UPLOAD_ON_MATCH
    checkbox gate + exception swallowing)
"""

import os
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATA_DIR", "/tmp/readest_upload_test")
os.environ.setdefault("BOOKS_DIR", "/tmp/readest_upload_test")


# ---------------------------------------------------------------------------
# EPUB fixture helper
# ---------------------------------------------------------------------------

_CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""


def _write_epub_fixture(
    dir_path: Path,
    filename: str,
    *,
    title: str = "Test Title",
    author: str = "Test Author",
    identifiers=("uuid:1234-5678-abcd", "calibre:11"),
) -> Path:
    """Build a real, minimal-but-valid EPUB on disk and return its path.

    Includes mimetype (stored uncompressed, first), META-INF/container.xml,
    an OPF carrying dc:title/dc:creator/dc:identifier, and a toc.ncx — ebooklib
    (used by `_read_epub_dc_metadata`) resolves every manifest href, so the
    referenced toc.ncx and content.xhtml must actually exist in the archive.
    Pass ``title=""`` to omit the <dc:title> element entirely (titleless book).
    """
    path = dir_path / filename
    identifier_els = "\n".join(
        (
            f'    <dc:identifier id="book-id">{ident}</dc:identifier>'
            if idx == 0
            else f"    <dc:identifier>{ident}</dc:identifier>"
        )
        for idx, ident in enumerate(identifiers)
    )
    title_el = f"    <dc:title>{title}</dc:title>" if title else ""
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:opf="http://www.idpf.org/2007/opf"
         unique-identifier="book-id">
  <metadata>
{identifier_els}
{title_el}
    <dc:creator opf:role="aut">{author}</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="content" href="content.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="content"/>
  </spine>
</package>"""
    toc_ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="book-id"/></head>
  <docTitle><text>Fixture</text></docTitle>
  <navMap>
    <navPoint id="navpoint-1" playOrder="1">
      <navLabel><text>Start</text></navLabel>
      <content src="content.xhtml"/>
    </navPoint>
  </navMap>
</ncx>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/content.xhtml", "<html><body><p>Hello</p></body></html>")
        zf.writestr("OEBPS/toc.ncx", toc_ncx)
    return path


def _make_client(book_hash: str = "hash1") -> MagicMock:
    """A ReadestClient mock defaulted to a clean "new book, upload succeeds" run.

    Individual tests override `pull_books`/`upload_file`/`list_files`/
    `cover_file_name`/`book_file_name` as needed for their scenario.
    """
    client = MagicMock()
    client.is_configured.return_value = True
    client.compute_book_hash.return_value = book_hash
    client.pull_books.return_value = []
    client.upload_file.return_value = {"usage": 1, "quota": 100}
    client.push_books.return_value = True
    return client


# ---------------------------------------------------------------------------
# 1. metaHash golden vector
# ---------------------------------------------------------------------------

class TestComputeMetaHashGoldenVector(unittest.TestCase):
    """`ReadestClient.compute_meta_hash` against a value captured live.

    This exact identifier list and title/author come from a real book in a
    live Readest account, and "f440c153f9345275034cba2e21cd4a0c" is the
    server's OWN stored `meta_hash` for that book (not a value we derived
    ourselves). If this test starts failing, our implementation has silently
    diverged from Readest's identity scheme, and cross-copy aggregation
    (matching different file copies of the same work into one Readest Book
    row) will break for every user.
    """

    TITLE = "The Virgin"
    AUTHORS = ["Wol-vriey"]
    IDENTIFIERS = [
        "ASIN:B087BPGDYR",
        "mobi-asin:B087BPGDYR",
        "calibre:11",
        "uuid:164cdb9d-a1d2-4f40-b9ac-e97913b38f3a",
        "uuid:c75f7f25-f888-4a95-b92b-7e0439cbf696",
    ]
    EXPECTED_HASH = "f440c153f9345275034cba2e21cd4a0c"

    def test_golden_vector_matches_live_readest_account(self):
        from src.api.readest_client import ReadestClient

        result = ReadestClient.compute_meta_hash(self.TITLE, self.AUTHORS, self.IDENTIFIERS)
        self.assertEqual(result, self.EXPECTED_HASH)

    def test_uuid_preference_rule_changes_hash_when_selected_uuid_differs(self):
        """The FIRST identifier mentioning "uuid" is the one selected (priority:
        uuid > calibre > isbn). Changing that selected uuid must change the
        hash — otherwise the scheme-priority selection has silently broken and
        every identifier is being folded in regardless of scheme."""
        from src.api.readest_client import ReadestClient

        changed_identifiers = list(self.IDENTIFIERS)
        # index 3 is the FIRST "uuid:" entry — the one the priority rule picks.
        changed_identifiers[3] = "uuid:00000000-0000-0000-0000-000000000000"

        changed = ReadestClient.compute_meta_hash(self.TITLE, self.AUTHORS, changed_identifiers)
        self.assertNotEqual(changed, self.EXPECTED_HASH)


# ---------------------------------------------------------------------------
# 2. CRITICAL REGRESSION: progress carry-over
# ---------------------------------------------------------------------------

class TestReadestUploadServiceProgressCarryOver(unittest.TestCase):
    """CRITICAL REGRESSION GUARD — confirmed against the live Readest API.

    Pushing a book record that omits `progress` silently NULLS the user's
    reading position server-side (unlike `readingStatus`, which resolves on
    its own field-level merge clock — see push_books' docstring in
    readest_client.py). `ReadestUploadService._publish_book` MUST copy the
    pulled server row's `progress` and reading-status fields forward into
    every pushed record whenever a server row already exists.

    This test MUST fail if that carry-over block is removed from
    `readest_upload_service.py`.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.fixture = _write_epub_fixture(Path(self._tmpdir.name), "book.epub")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_progress_and_reading_status_carried_forward_on_update(self):
        from src.services.readest_upload_service import ReadestUploadService

        client = _make_client(book_hash="hash1")
        client.upload_file.return_value = {"usage": 5, "quota": 50}
        client.pull_books.return_value = [
            {
                "book_hash": "hash1",
                "progress": [42, 100],
                "reading_status": "reading",
                "reading_status_updated_at": "2026-08-20T10:00:00Z",
                "uploaded_at": "2026-08-01T00:00:00Z",
                "created_at": "2026-07-01T00:00:00Z",
                # deliberately no group_name/group_id: the "NO group" case.
            }
        ]

        parser = MagicMock()
        parser.resolve_book_path.return_value = str(self.fixture)
        service = ReadestUploadService(client, parser)

        service.publish_book("book.epub")

        client.push_books.assert_called_once()
        record = client.push_books.call_args[0][0][0]
        self.assertEqual(
            record["progress"],
            [42, 100],
            "progress must be carried over from the pulled server row — an "
            "update that omits it silently wipes the user's reading position",
        )
        self.assertEqual(record["readingStatus"], "reading")


# ---------------------------------------------------------------------------
# 3. Group assignment vs preservation
# ---------------------------------------------------------------------------

class TestReadestUploadServiceGroupAssignment(unittest.TestCase):
    def setUp(self):
        self._saved_group_env = os.environ.pop("READEST_GROUP_NAME", None)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.fixture = _write_epub_fixture(Path(self._tmpdir.name), "book.epub")

    def tearDown(self):
        self._tmpdir.cleanup()
        if self._saved_group_env is not None:
            os.environ["READEST_GROUP_NAME"] = self._saved_group_env
        else:
            os.environ.pop("READEST_GROUP_NAME", None)

    def _service(self, client):
        from src.services.readest_upload_service import ReadestUploadService

        parser = MagicMock()
        parser.resolve_book_path.return_value = str(self.fixture)
        return ReadestUploadService(client, parser)

    def test_new_book_gets_default_group_bookbridge(self):
        client = _make_client(book_hash="hash1")
        client.pull_books.return_value = []
        service = self._service(client)

        result = service.publish_book("book.epub")

        self.assertEqual(result.status, "created")
        record = client.push_books.call_args[0][0][0]
        self.assertEqual(record["groupName"], "BookBridge")
        self.assertEqual(record["groupId"], str(uuid.uuid5(uuid.NAMESPACE_URL, "BookBridge")))

    def test_existing_group_is_preserved_not_stomped(self):
        client = _make_client(book_hash="hash1")
        client.pull_books.return_value = [
            {
                "book_hash": "hash1",
                "group_name": "My Shelf",
                "group_id": "existing-group-id-123",
                "uploaded_at": None,
                "created_at": None,
                "progress": None,
            }
        ]
        service = self._service(client)

        result = service.publish_book("book.epub")

        self.assertEqual(result.status, "updated")
        record = client.push_books.call_args[0][0][0]
        self.assertEqual(
            record["groupName"], "My Shelf",
            "the user's own filing in Readest must never be stomped by a republish",
        )
        self.assertEqual(record["groupId"], "existing-group-id-123")


# ---------------------------------------------------------------------------
# 4. Cloud-presence detection (uploaded_at is not proof)
# ---------------------------------------------------------------------------

class TestReadestUploadServiceCloudPresenceDetection(unittest.TestCase):
    BOOK_HASH = "hash1"
    COVER_KEY = f"Readest/Books/{BOOK_HASH}/cover.png"
    BOOK_KEY = f"Readest/Books/{BOOK_HASH}/{BOOK_HASH}.epub"

    def setUp(self):
        self._saved_group_env = os.environ.pop("READEST_GROUP_NAME", None)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.fixture = _write_epub_fixture(Path(self._tmpdir.name), "book.epub")

    def tearDown(self):
        self._tmpdir.cleanup()
        if self._saved_group_env is not None:
            os.environ["READEST_GROUP_NAME"] = self._saved_group_env
        else:
            os.environ.pop("READEST_GROUP_NAME", None)

    def _service(self, client):
        from src.services.readest_upload_service import ReadestUploadService

        parser = MagicMock()
        parser.resolve_book_path.return_value = str(self.fixture)
        return ReadestUploadService(client, parser)

    def _base_row(self, group_name: str) -> dict:
        return {
            "book_hash": self.BOOK_HASH,
            "group_name": group_name,
            "group_id": "grp-1",
            "uploaded_at": "2026-08-01T00:00:00Z",
            "deleted_at": None,
            "created_at": "2026-07-01T00:00:00Z",
        }

    def test_matching_group_and_present_book_blob_is_skipped(self):
        client = _make_client(book_hash=self.BOOK_HASH)
        client.pull_books.return_value = [self._base_row(group_name="BookBridge")]
        client.cover_file_name.return_value = self.COVER_KEY
        client.list_files.return_value = [{"file_key": self.BOOK_KEY}]
        service = self._service(client)

        result = service.publish_book("book.epub", group_name="BookBridge")

        self.assertEqual(result.status, "skipped")
        client.push_books.assert_not_called()

    def test_only_cover_present_triggers_reupload_of_book_bytes(self):
        client = _make_client(book_hash=self.BOOK_HASH)
        client.pull_books.return_value = [self._base_row(group_name="Some Other Group")]
        client.cover_file_name.return_value = self.COVER_KEY
        # list_files sees ONLY the cover blob — the book's own bytes are absent.
        client.list_files.return_value = [{"file_key": self.COVER_KEY}]
        client.book_file_name.return_value = self.BOOK_KEY
        service = self._service(client)

        result = service.publish_book("book.epub")

        client.upload_file.assert_called_once()
        self.assertEqual(
            client.upload_file.call_args[0][0], self.BOOK_KEY,
            "the book blob (not just the cover) must be re-uploaded when storage "
            "shows only a cover entry",
        )
        self.assertEqual(result.status, "updated")

    def test_list_files_lookup_failure_falls_back_to_trusting_uploaded_at(self):
        client = _make_client(book_hash=self.BOOK_HASH)
        client.pull_books.return_value = [self._base_row(group_name="Some Other Group")]
        client.cover_file_name.return_value = self.COVER_KEY
        client.list_files.return_value = None  # the storage lookup itself failed
        service = self._service(client)

        result = service.publish_book("book.epub")

        client.upload_file.assert_not_called()
        self.assertEqual(result.status, "updated")


# ---------------------------------------------------------------------------
# 5. Quota / upload failure
# ---------------------------------------------------------------------------

class TestReadestUploadServiceUploadFailure(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.fixture = _write_epub_fixture(Path(self._tmpdir.name), "book.epub")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_upload_failure_returns_error_and_does_not_push(self):
        from src.services.readest_upload_service import ReadestUploadService

        client = _make_client()
        client.upload_file.return_value = None  # quota exhausted / storage error

        parser = MagicMock()
        parser.resolve_book_path.return_value = str(self.fixture)
        service = ReadestUploadService(client, parser)

        result = service.publish_book("book.epub")

        self.assertEqual(result.status, "error")
        client.push_books.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Early-outs
# ---------------------------------------------------------------------------

class TestReadestUploadServiceEarlyOuts(unittest.TestCase):
    def test_not_configured_returns_disabled(self):
        from src.services.readest_upload_service import ReadestUploadService

        client = MagicMock()
        client.is_configured.return_value = False
        parser = MagicMock()
        service = ReadestUploadService(client, parser)

        result = service.publish_book("book.epub")

        self.assertEqual(result.status, "disabled")
        parser.resolve_book_path.assert_not_called()

    def test_unsupported_format_is_skipped(self):
        from src.services.readest_upload_service import ReadestUploadService

        client = _make_client()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 fake pdf content")
            pdf_path = f.name
        try:
            parser = MagicMock()
            parser.resolve_book_path.return_value = pdf_path
            service = ReadestUploadService(client, parser)

            result = service.publish_book("book.pdf")

            self.assertEqual(result.status, "skipped")
            client.push_books.assert_not_called()
        finally:
            os.unlink(pdf_path)


# ---------------------------------------------------------------------------
# 7. Metadata derivation from the EPUB
# ---------------------------------------------------------------------------

class TestReadestUploadServiceMetadataDerivation(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _service(self, client, fixture_path):
        from src.services.readest_upload_service import ReadestUploadService

        parser = MagicMock()
        parser.resolve_book_path.return_value = str(fixture_path)
        return ReadestUploadService(client, parser)

    def test_derives_title_and_author_from_epub_dc_metadata(self):
        fixture = _write_epub_fixture(
            self.dir_path, "book.epub",
            title="My Book Title", author="Jane Author",
            identifiers=("uuid:1234-5678-abcd", "calibre:11"),
        )
        client = _make_client()
        service = self._service(client, fixture)

        result = service.publish_book("book.epub")

        self.assertEqual(result.status, "created")
        client.push_books.assert_called_once()
        record = client.push_books.call_args[0][0][0]
        self.assertEqual(record["title"], "My Book Title")
        self.assertEqual(record["author"], "Jane Author")

    def test_titleless_epub_falls_back_to_file_stem(self):
        fixture = _write_epub_fixture(
            self.dir_path, "notitle.epub",
            title="", author="Jane Author",
            identifiers=("uuid:no-title-book",),
        )
        client = _make_client()
        service = self._service(client, fixture)

        result = service.publish_book("notitle.epub")

        self.assertEqual(result.status, "created")
        record = client.push_books.call_args[0][0][0]
        self.assertEqual(record["title"], fixture.stem)


# ---------------------------------------------------------------------------
# 8. Hook gating in src/web_server.py — the "on" checkbox bug
# ---------------------------------------------------------------------------

class TestPublishSavedEbookToReadestHook(unittest.TestCase):
    """`_publish_saved_ebook_to_readest` gating in src/web_server.py.

    Guards failure mode #1 from CLAUDE.md: settings checkboxes POST the
    literal string "on" when checked (and are absent, not "false", when
    unchecked) — the gate must treat "on" as enabled, not just "true", and
    must default closed when the setting is missing.
    """

    def _book(self):
        return SimpleNamespace(original_ebook_filename="foo.epub", ebook_filename="foo.epub")

    def _run(self, setting_value, service_side_effect=None):
        """Run the hook with `user_setting` returning `setting_value`.

        Patches `user_setting`, `container`, and both Readest classes so no
        network call is possible; returns the mocked ReadestUploadService
        class so callers can assert on construction/`publish_book` calls.
        """
        from src.web_server import _publish_saved_ebook_to_readest

        with patch("src.web_server.user_setting", return_value=setting_value), \
                patch("src.web_server.container"), \
                patch("src.api.readest_client.ReadestClient"), \
                patch("src.services.readest_upload_service.ReadestUploadService") as MockService:
            if service_side_effect is not None:
                MockService.return_value.publish_book.side_effect = service_side_effect
            else:
                MockService.return_value.publish_book.return_value = SimpleNamespace(
                    status="created", message="ok",
                )
            _publish_saved_ebook_to_readest(self._book())
            return MockService

    def test_checkbox_on_enables_publish(self):
        MockService = self._run("on")
        MockService.assert_called_once()
        MockService.return_value.publish_book.assert_called_once_with("foo.epub")

    def test_true_string_enables_publish(self):
        MockService = self._run("true")
        MockService.return_value.publish_book.assert_called_once_with("foo.epub")

    def test_false_string_disables_publish(self):
        MockService = self._run("false")
        MockService.assert_not_called()

    def test_empty_string_disables_publish(self):
        MockService = self._run("")
        MockService.assert_not_called()

    def test_absent_setting_disables_publish(self):
        # Simulates no configured value anywhere: user_setting returns None.
        MockService = self._run(None)
        MockService.assert_not_called()

    def test_swallows_service_exceptions(self):
        try:
            self._run("on", service_side_effect=RuntimeError("boom"))
        except Exception as e:  # pragma: no cover - the assertion IS that this doesn't happen
            self.fail(f"_publish_saved_ebook_to_readest propagated an exception: {e}")


# ---------------------------------------------------------------------------
# 9. publish_reading_books — the "391 books, only 4 in progress" safety sweep
# ---------------------------------------------------------------------------

def _make_db(books=None, states=None) -> MagicMock:
    """A DatabaseService mock returning the given catalog/state rows for
    `get_books_by_status`/`get_all_states`."""
    db = MagicMock()
    db.get_books_by_status.return_value = list(books or [])
    db.get_all_states.return_value = list(states or [])
    return db


def _make_book(abs_id: str, *, original: str = None, fallback: str = None) -> SimpleNamespace:
    """A lightweight Book stand-in carrying only the attributes
    `publish_reading_books` actually reads (`abs_id`, `original_ebook_filename`,
    `ebook_filename`)."""
    return SimpleNamespace(abs_id=abs_id, original_ebook_filename=original, ebook_filename=fallback)


def _make_state(abs_id: str, percentage) -> SimpleNamespace:
    """A lightweight State stand-in carrying only `abs_id`/`percentage`."""
    return SimpleNamespace(abs_id=abs_id, percentage=percentage)


_ZERO_SUMMARY = {
    "candidates": 0,
    "uploaded": 0,
    "skipped_present": 0,
    "errors": 0,
    "capped": False,
}


class TestPublishReadingBooksGate(unittest.TestCase):
    """The candidate gate itself: only a book with a non-zero, non-complete
    reading position becomes a sweep candidate.

    This is the test proving a 391-book library does not become 391 uploads —
    of a never-started book, a mid-book, and a finished book, only the
    mid-book one is ever passed to `publish_book`.
    """

    def test_only_mid_progress_book_is_a_candidate(self):
        from src.services.readest_upload_service import ReadestPublishResult, ReadestUploadService

        books = [
            _make_book("abs-never-started", original="never.epub"),
            _make_book("abs-mid-book", original="mid.epub"),
            _make_book("abs-finished", original="finished.epub"),
        ]
        states = [
            _make_state("abs-never-started", 0.0),
            _make_state("abs-mid-book", 0.5),
            _make_state("abs-finished", 0.995),
        ]
        client = _make_client()
        parser = MagicMock()
        parser.resolve_book_path.return_value = "/fake/mid.epub"
        db = _make_db(books, states)
        service = ReadestUploadService(client, parser, database_service=db)
        service.publish_book = MagicMock(
            return_value=ReadestPublishResult(status="created", book_hash="h")
        )

        summary = service.publish_reading_books(user_id=1)

        service.publish_book.assert_called_once_with("mid.epub")
        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["uploaded"], 1)


class TestPublishReadingBooksUnitConfusion(unittest.TestCase):
    """CRITICAL REGRESSION GUARD — unit confusion between percent and fraction.

    `SYNC_COMPLETION_THRESHOLD` is expressed in PERCENT (default "99"), while
    `State.percentage` is a 0-1 FRACTION. `publish_reading_books` must divide
    the threshold by 100 before comparing it against `percentage`.

    If that conversion were ever dropped and the raw fraction were compared
    against the raw percent instead (e.g. `0.98 < 99`), EVERY real book would
    look unfinished — a 0-1 fraction is always less than a percent value of
    99 or 50 — and the sweep would upload the entire catalogue, which is
    exactly the outcome this whole gate exists to prevent.
    """

    def setUp(self):
        self._saved_threshold = os.environ.pop("SYNC_COMPLETION_THRESHOLD", None)

    def tearDown(self):
        if self._saved_threshold is not None:
            os.environ["SYNC_COMPLETION_THRESHOLD"] = self._saved_threshold
        else:
            os.environ.pop("SYNC_COMPLETION_THRESHOLD", None)

    def _run(self, percentage):
        from src.services.readest_upload_service import ReadestPublishResult, ReadestUploadService

        books = [_make_book("abs-1", original="book.epub")]
        states = [_make_state("abs-1", percentage)]
        client = _make_client()
        parser = MagicMock()
        parser.resolve_book_path.return_value = "/fake/book.epub"
        db = _make_db(books, states)
        service = ReadestUploadService(client, parser, database_service=db)
        service.publish_book = MagicMock(
            return_value=ReadestPublishResult(status="created", book_hash="h")
        )
        summary = service.publish_reading_books(user_id=1)
        return summary, service.publish_book

    def test_default_threshold_98_percent_uploaded_995_percent_not(self):
        os.environ.pop("SYNC_COMPLETION_THRESHOLD", None)  # exercise the "99" default

        summary, publish_book = self._run(0.98)
        publish_book.assert_called_once_with("book.epub")
        self.assertEqual(summary["uploaded"], 1)

        summary, publish_book = self._run(0.995)
        publish_book.assert_not_called()
        self.assertEqual(summary["candidates"], 0)

    def test_overridden_threshold_moves_the_boundary(self):
        os.environ["SYNC_COMPLETION_THRESHOLD"] = "50"

        # 98% is now well past a 50% threshold — no longer uploaded.
        summary, publish_book = self._run(0.98)
        publish_book.assert_not_called()
        self.assertEqual(summary["candidates"], 0)

        # 40% is now below the lowered threshold — uploaded.
        summary, publish_book = self._run(0.4)
        publish_book.assert_called_once_with("book.epub")
        self.assertEqual(summary["uploaded"], 1)


class TestPublishReadingBooksSinglePullBooksCall(unittest.TestCase):
    """Per-book-cost regression guard: a real cloud listing runs 400+ rows,
    so the sweep must call `pull_books` exactly ONCE for the whole run —
    never once per candidate — or it becomes a network storm."""

    def test_pull_books_called_once_with_since_zero(self):
        from src.services.readest_upload_service import ReadestPublishResult, ReadestUploadService

        books = [_make_book(f"abs-{i}", original=f"book{i}.epub") for i in range(4)]
        states = [_make_state(f"abs-{i}", 0.5) for i in range(4)]
        client = _make_client()
        parser = MagicMock()
        parser.resolve_book_path.side_effect = lambda fn: f"/fake/{fn}"
        db = _make_db(books, states)
        service = ReadestUploadService(client, parser, database_service=db)
        service.publish_book = MagicMock(
            return_value=ReadestPublishResult(status="created", book_hash="h")
        )

        service.publish_reading_books(user_id=1)

        client.pull_books.assert_called_once_with(since=0)


class TestPublishReadingBooksAlreadyPresent(unittest.TestCase):
    def test_present_hash_is_skipped_without_calling_publish_book(self):
        from src.services.readest_upload_service import ReadestPublishResult, ReadestUploadService

        books = [_make_book("abs-1", original="book.epub")]
        states = [_make_state("abs-1", 0.5)]
        client = _make_client()
        client.compute_book_hash.return_value = "hash-present"
        client.pull_books.return_value = [{"book_hash": "hash-present", "deleted_at": None}]
        parser = MagicMock()
        parser.resolve_book_path.return_value = "/fake/book.epub"
        db = _make_db(books, states)
        service = ReadestUploadService(client, parser, database_service=db)
        service.publish_book = MagicMock(
            return_value=ReadestPublishResult(status="created", book_hash="h")
        )

        summary = service.publish_reading_books(user_id=1)

        service.publish_book.assert_not_called()
        self.assertEqual(summary["skipped_present"], 1)
        self.assertEqual(summary["uploaded"], 0)


class TestPublishReadingBooksSoftDeletedNotPresent(unittest.TestCase):
    def test_soft_deleted_cloud_row_does_not_count_as_present(self):
        from src.services.readest_upload_service import ReadestPublishResult, ReadestUploadService

        books = [_make_book("abs-1", original="book.epub")]
        states = [_make_state("abs-1", 0.5)]
        client = _make_client()
        client.compute_book_hash.return_value = "hash-deleted"
        client.pull_books.return_value = [
            {"book_hash": "hash-deleted", "deleted_at": "2026-08-01T00:00:00Z"}
        ]
        parser = MagicMock()
        parser.resolve_book_path.return_value = "/fake/book.epub"
        db = _make_db(books, states)
        service = ReadestUploadService(client, parser, database_service=db)
        service.publish_book = MagicMock(
            return_value=ReadestPublishResult(status="created", book_hash="h")
        )

        summary = service.publish_reading_books(user_id=1)

        service.publish_book.assert_called_once_with("book.epub")
        self.assertEqual(summary["skipped_present"], 0)
        self.assertEqual(summary["uploaded"], 1)


class TestPublishReadingBooksCap(unittest.TestCase):
    def setUp(self):
        self._saved_cap = os.environ.pop("READEST_UPLOAD_MAX_PER_RUN", None)

    def tearDown(self):
        if self._saved_cap is not None:
            os.environ["READEST_UPLOAD_MAX_PER_RUN"] = self._saved_cap
        else:
            os.environ.pop("READEST_UPLOAD_MAX_PER_RUN", None)

    def test_cap_stops_uploads_and_skips_do_not_consume_it(self):
        from src.services.readest_upload_service import ReadestPublishResult, ReadestUploadService

        os.environ["READEST_UPLOAD_MAX_PER_RUN"] = "2"

        # candidate 0 is already present (a free skip); 1-3 are fresh uploads.
        books = [_make_book(f"abs-{i}", original=f"book{i}.epub") for i in range(4)]
        states = [_make_state(f"abs-{i}", 0.5) for i in range(4)]
        client = _make_client()
        parser = MagicMock()
        parser.resolve_book_path.side_effect = lambda fn: f"/fake/{fn}"
        client.compute_book_hash.side_effect = lambda resolved: f"hash-{resolved}"
        client.pull_books.return_value = [{"book_hash": "hash-/fake/book0.epub", "deleted_at": None}]
        db = _make_db(books, states)
        service = ReadestUploadService(client, parser, database_service=db)
        service.publish_book = MagicMock(
            return_value=ReadestPublishResult(status="created", book_hash="h")
        )

        summary = service.publish_reading_books(user_id=1)

        self.assertEqual(summary["skipped_present"], 1)
        self.assertEqual(summary["uploaded"], 2)
        self.assertTrue(summary["capped"])
        self.assertEqual(
            service.publish_book.call_count, 2,
            "the free skip must not eat into the upload cap budget",
        )


class TestPublishReadingBooksSkippedIsNotAnError(unittest.TestCase):
    def test_publish_book_skipped_counts_as_skip_not_error(self):
        """A `skipped` result from publish_book must not inflate the error count.

        `publish_book` runs its own cloud-presence check, so it can legitimately
        conclude "already there" for a book the sweep's hash pre-filter did not
        catch. Counting that as an error makes a completely healthy sweep report
        failures in its summary log line, which is what diagnostics reads.
        """
        from src.services.readest_upload_service import ReadestPublishResult, ReadestUploadService

        books = [_make_book("abs-0", original="book0.epub")]
        states = [_make_state("abs-0", 0.5)]
        client = _make_client()
        parser = MagicMock()
        parser.resolve_book_path.side_effect = lambda fn: f"/fake/{fn}"
        client.compute_book_hash.side_effect = lambda resolved: f"hash-{resolved}"
        client.pull_books.return_value = []
        service = ReadestUploadService(client, parser, database_service=_make_db(books, states))
        service.publish_book = MagicMock(
            return_value=ReadestPublishResult(status="skipped", book_hash="h")
        )

        summary = service.publish_reading_books(user_id=1)

        self.assertEqual(summary["errors"], 0, "a skip is not a failure")
        self.assertEqual(summary["skipped_present"], 1)
        self.assertEqual(summary["uploaded"], 0)


class TestPublishReadingBooksErrorStopsSweep(unittest.TestCase):
    def test_error_status_from_publish_book_stops_further_calls(self):
        from src.services.readest_upload_service import ReadestPublishResult, ReadestUploadService

        books = [_make_book(f"abs-{i}", original=f"book{i}.epub") for i in range(3)]
        states = [_make_state(f"abs-{i}", 0.5) for i in range(3)]
        client = _make_client()
        parser = MagicMock()
        parser.resolve_book_path.side_effect = lambda fn: f"/fake/{fn}"
        client.compute_book_hash.side_effect = lambda resolved: f"hash-{resolved}"
        db = _make_db(books, states)
        service = ReadestUploadService(client, parser, database_service=db)
        service.publish_book = MagicMock(
            side_effect=[
                ReadestPublishResult(status="error", message="quota exhausted"),
                ReadestPublishResult(status="created", book_hash="h"),
                ReadestPublishResult(status="created", book_hash="h"),
            ]
        )

        summary = service.publish_reading_books(user_id=1)

        self.assertEqual(
            service.publish_book.call_count, 1,
            "an error status must stop the sweep, not just the one book",
        )
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["uploaded"], 0)


class TestPublishReadingBooksEarlyOuts(unittest.TestCase):
    """Defensive early-outs. Each case asserts NO network call was made —
    proxied by `pull_books`/`get_books_by_status` (both mocked, so no real
    request is ever possible) never firing."""

    def setUp(self):
        self._saved_cap = os.environ.pop("READEST_UPLOAD_MAX_PER_RUN", None)

    def tearDown(self):
        if self._saved_cap is not None:
            os.environ["READEST_UPLOAD_MAX_PER_RUN"] = self._saved_cap
        else:
            os.environ.pop("READEST_UPLOAD_MAX_PER_RUN", None)

    def test_not_configured_makes_no_calls(self):
        from src.services.readest_upload_service import ReadestUploadService

        client = MagicMock()
        client.is_configured.return_value = False
        parser = MagicMock()
        db = _make_db([_make_book("abs-1", original="book.epub")], [_make_state("abs-1", 0.5)])
        service = ReadestUploadService(client, parser, database_service=db)

        summary = service.publish_reading_books(user_id=1)

        self.assertEqual(summary, _ZERO_SUMMARY)
        db.get_books_by_status.assert_not_called()
        client.pull_books.assert_not_called()

    def test_zero_cap_makes_no_calls(self):
        from src.services.readest_upload_service import ReadestUploadService

        os.environ["READEST_UPLOAD_MAX_PER_RUN"] = "0"
        client = _make_client()
        parser = MagicMock()
        db = _make_db([_make_book("abs-1", original="book.epub")], [_make_state("abs-1", 0.5)])
        service = ReadestUploadService(client, parser, database_service=db)

        summary = service.publish_reading_books(user_id=1)

        self.assertEqual(summary, _ZERO_SUMMARY)
        db.get_books_by_status.assert_not_called()
        client.pull_books.assert_not_called()

    def test_no_candidates_makes_no_pull_books_call(self):
        from src.services.readest_upload_service import ReadestUploadService

        client = _make_client()
        parser = MagicMock()
        # Neither book qualifies: one never started, one finished.
        books = [_make_book("abs-1", original="book1.epub"), _make_book("abs-2", original="book2.epub")]
        states = [_make_state("abs-1", 0.0), _make_state("abs-2", 0.995)]
        db = _make_db(books, states)
        service = ReadestUploadService(client, parser, database_service=db)

        summary = service.publish_reading_books(user_id=1)

        self.assertEqual(summary, _ZERO_SUMMARY)
        client.pull_books.assert_not_called()

    def test_pull_books_returning_none_yields_zero_summary_and_uploads_nothing(self):
        from src.services.readest_upload_service import ReadestPublishResult, ReadestUploadService

        client = _make_client()
        client.pull_books.return_value = None
        parser = MagicMock()
        parser.resolve_book_path.return_value = "/fake/book.epub"
        db = _make_db([_make_book("abs-1", original="book.epub")], [_make_state("abs-1", 0.5)])
        service = ReadestUploadService(client, parser, database_service=db)
        service.publish_book = MagicMock(
            return_value=ReadestPublishResult(status="created", book_hash="h")
        )

        summary = service.publish_reading_books(user_id=1)

        self.assertEqual(summary, _ZERO_SUMMARY)
        service.publish_book.assert_not_called()


class TestPublishReadingBooksFilenameFiltering(unittest.TestCase):
    def test_storyteller_artifact_and_non_epub_are_excluded(self):
        from src.services.readest_upload_service import ReadestPublishResult, ReadestUploadService

        books = [
            _make_book(
                "abs-storyteller",
                original="storyteller_1234abcd-ab12-40aa-9999-abcdefabcdef.epub",
            ),
            _make_book("abs-pdf", original="book.pdf"),
        ]
        states = [
            _make_state("abs-storyteller", 0.5),
            _make_state("abs-pdf", 0.5),
        ]
        client = _make_client()
        parser = MagicMock()
        db = _make_db(books, states)
        service = ReadestUploadService(client, parser, database_service=db)
        service.publish_book = MagicMock(
            return_value=ReadestPublishResult(status="created", book_hash="h")
        )

        summary = service.publish_reading_books(user_id=1)

        service.publish_book.assert_not_called()
        client.pull_books.assert_not_called()
        self.assertEqual(summary["candidates"], 0)

    def test_original_ebook_filename_preferred_over_ebook_filename(self):
        from src.services.readest_upload_service import ReadestPublishResult, ReadestUploadService

        books = [
            _make_book("abs-both", original="preferred.epub", fallback="fallback.epub"),
            _make_book("abs-fallback-only", original=None, fallback="fallback_only.epub"),
        ]
        states = [
            _make_state("abs-both", 0.5),
            _make_state("abs-fallback-only", 0.5),
        ]
        client = _make_client()
        parser = MagicMock()
        parser.resolve_book_path.side_effect = lambda fn: f"/fake/{fn}"
        db = _make_db(books, states)
        service = ReadestUploadService(client, parser, database_service=db)
        service.publish_book = MagicMock(
            return_value=ReadestPublishResult(status="created", book_hash="h")
        )

        service.publish_reading_books(user_id=1)

        called_filenames = {c.args[0] for c in service.publish_book.call_args_list}
        self.assertIn(
            "preferred.epub", called_filenames,
            "original_ebook_filename must be preferred over ebook_filename",
        )
        self.assertNotIn("fallback.epub", called_filenames)
        self.assertIn(
            "fallback_only.epub", called_filenames,
            "ebook_filename must still be used as a fallback when "
            "original_ebook_filename is absent",
        )


class TestPublishReadingBooksPerCandidateIsolation(unittest.TestCase):
    def test_one_candidate_erroring_does_not_stop_the_rest(self):
        from src.services.readest_upload_service import ReadestPublishResult, ReadestUploadService

        books = [
            _make_book("abs-missing", original="missing.epub"),
            _make_book("abs-ok", original="ok.epub"),
        ]
        states = [
            _make_state("abs-missing", 0.5),
            _make_state("abs-ok", 0.5),
        ]
        client = _make_client()
        parser = MagicMock()
        parser.resolve_book_path.side_effect = [
            FileNotFoundError("could not locate missing.epub"),
            "/fake/ok.epub",
        ]
        db = _make_db(books, states)
        service = ReadestUploadService(client, parser, database_service=db)
        service.publish_book = MagicMock(
            return_value=ReadestPublishResult(status="created", book_hash="h")
        )

        summary = service.publish_reading_books(user_id=1)

        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["uploaded"], 1)
        service.publish_book.assert_called_once_with("ok.epub")


if __name__ == "__main__":
    unittest.main()

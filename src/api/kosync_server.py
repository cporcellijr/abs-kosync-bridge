# KoSync Server - Extracted from web_server.py for clean code separation
# Implements KOSync protocol compatible with kosync-dotnet
import hashlib
import logging
import os
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

# Create Blueprint for KoSync endpoints
kosync_bp = Blueprint('kosync', __name__)

# Module-level references - set via init_kosync_server()
_database_service = None
_container = None
_manager = None
_hash_cache = None
_ebook_dir = None
_active_scans = set()


def init_kosync_server(database_service, container, manager, hash_cache=None, ebook_dir=None):
    """Initialize KoSync server with required dependencies."""
    global _database_service, _container, _manager, _hash_cache, _ebook_dir
    _database_service = database_service
    _container = container
    _manager = manager
    _hash_cache = hash_cache
    _ebook_dir = ebook_dir


def kosync_auth_required(f):
    """Decorator for KOSync authentication."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = request.headers.get('x-auth-user')
        key = request.headers.get('x-auth-key')

        expected_user = os.environ.get("KOSYNC_USER")
        expected_password = os.environ.get("KOSYNC_KEY")

        if not expected_user or not expected_password:
            logger.error("KOSync Integrated Server: Credentials not configured in settings")
            return jsonify({"error": "Server not configured"}), 500

        expected_hash = hashlib.md5(expected_password.encode()).hexdigest()

        if user and expected_user and user.lower() == expected_user.lower() and (key == expected_password or key == expected_hash):
            return f(*args, **kwargs)

        logger.warning(f"KOSync Integrated Server: Unauthorized access attempt from {request.remote_addr} (user: {user})")
        return jsonify({"error": "Unauthorized"}), 401
    return decorated_function


# ---------------- KOSync Protocol Endpoints ----------------

@kosync_bp.route('/healthcheck')
@kosync_bp.route('/koreader/healthcheck')
def kosync_healthcheck():
    """KOSync connectivity check"""
    return "OK", 200


@kosync_bp.route('/users/auth', methods=['GET'])
@kosync_bp.route('/koreader/users/auth', methods=['GET'])
def kosync_users_auth():
    """KOReader auth check - validates credentials per kosync-dotnet spec"""
    user = request.headers.get('x-auth-user')
    key = request.headers.get('x-auth-key')

    expected_user = os.environ.get("KOSYNC_USER")
    expected_password = os.environ.get("KOSYNC_KEY")

    if not user or not key:
        logger.warning(f"KOSync Auth: Missing credentials from {request.remote_addr}")
        return jsonify({"message": "Invalid credentials"}), 401

    if not expected_user or not expected_password:
        logger.error("KOSync Auth: Server credentials not configured")
        return jsonify({"message": "Server not configured"}), 500

    expected_hash = hashlib.md5(expected_password.encode()).hexdigest()

    if user.lower() == expected_user.lower() and (key == expected_password or key == expected_hash):
        logger.debug(f"KOSync Auth: User '{user}' authenticated successfully")
        return jsonify({"username": user}), 200

    logger.warning(f"KOSync Auth: Failed auth attempt for user '{user}' from {request.remote_addr}")
    return jsonify({"message": "Unauthorized"}), 401


@kosync_bp.route('/users/create', methods=['POST'])
@kosync_bp.route('/koreader/users/create', methods=['POST'])
def kosync_users_create():
    """Stub for KOReader user registration check"""
    return jsonify({
        "id": 1,
        "username": os.environ.get("KOSYNC_USER", "user")
    }), 201


@kosync_bp.route('/users/login', methods=['POST'])
@kosync_bp.route('/koreader/users/login', methods=['POST'])
def kosync_users_login():
    """Stub for KOReader login check"""
    return jsonify({
        "id": 1,
        "username": os.environ.get("KOSYNC_USER", "user"),
        "active": True,
        "token": os.environ.get("KOSYNC_KEY", "")
    }), 200


@kosync_bp.route('/syncs/progress/<doc_id>', methods=['GET'])
@kosync_bp.route('/koreader/syncs/progress/<doc_id>', methods=['GET'])
@kosync_auth_required
def kosync_get_progress(doc_id):
    """
    Fetch progress for a specific document.
    Returns 502 (not 404) if document not found, per kosync-dotnet spec.
    """
    kosync_doc = _database_service.get_kosync_document(doc_id)

    if kosync_doc:
        return jsonify({
            "device": kosync_doc.device or "",
            "device_id": kosync_doc.device_id or "",
            "document": kosync_doc.document_hash,
            "percentage": float(kosync_doc.percentage) if kosync_doc.percentage else 0,
            "progress": kosync_doc.progress or "",
            "timestamp": int(kosync_doc.timestamp.timestamp()) if kosync_doc.timestamp else 0
        }), 200

    # Fallback: Check mapped book with State data
    book = _database_service.get_book_by_kosync_id(doc_id)
    if book:
        states = _database_service.get_states_for_book(book.abs_id)
        if not states:
            return jsonify({"message": "Document not found on server"}), 502

        kosync_state = next((s for s in states if s.client_name.lower() == 'kosync'), None)
        if kosync_state:
            latest_state = kosync_state
        else:
            latest_state = max(states, key=lambda s: s.last_updated if s.last_updated else 0)

        return jsonify({
            "device": "abs-kosync-bridge",
            "device_id": "abs-kosync-bridge",
            "document": doc_id,
            "percentage": float(latest_state.percentage) if latest_state.percentage else 0,
            "progress": (latest_state.xpath or latest_state.cfi) if hasattr(latest_state, 'xpath') else "",
            "timestamp": int(latest_state.last_updated) if latest_state.last_updated else 0
        }), 200

    logger.debug(f"KOSync: Document not found: {doc_id[:8]}...")
    return jsonify({"message": "Document not found on server"}), 502


@kosync_bp.route('/syncs/progress', methods=['PUT'])
@kosync_bp.route('/koreader/syncs/progress', methods=['PUT'])
@kosync_auth_required
def kosync_put_progress():
    """
    Receive progress update from KOReader.
    Stores ALL documents, whether mapped to ABS or not.
    """
    from flask import current_app
    from src.db.models import KosyncDocument, State, Book

    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400

    doc_hash = data.get('document')
    if not doc_hash:
        return jsonify({"error": "Missing document ID"}), 400

    percentage = data.get('percentage', 0)
    progress = data.get('progress', '')
    device = data.get('device', '')
    device_id = data.get('device_id', '')

    now = datetime.utcnow()

    kosync_doc = _database_service.get_kosync_document(doc_hash)

    # Optional "furthest wins" protection
    furthest_wins = os.environ.get('KOSYNC_FURTHEST_WINS', 'true').lower() == 'true'
    if furthest_wins and kosync_doc and kosync_doc.percentage:
        existing_pct = float(kosync_doc.percentage)
        new_pct = float(percentage)

        if new_pct < existing_pct - 0.0001:
            logger.debug(f"KOSync: Rejecting backwards progress {new_pct:.4f} < {existing_pct:.4f} for {doc_hash[:8]}")
            return jsonify({
                "document": doc_hash,
                "timestamp": kosync_doc.timestamp.isoformat() + "Z" if kosync_doc.timestamp else now.isoformat() + "Z"
            }), 200

    if kosync_doc is None:
        kosync_doc = KosyncDocument(
            document_hash=doc_hash,
            progress=progress,
            percentage=percentage,
            device=device,
            device_id=device_id,
            timestamp=now
        )
        logger.info(f"KOSync: New document tracked: {doc_hash[:8]}... from device '{device}'")
    else:
        kosync_doc.progress = progress
        kosync_doc.percentage = percentage
        kosync_doc.device = device
        kosync_doc.device_id = device_id
        kosync_doc.timestamp = now
        logger.debug(f"KOSync: Syncing document: {doc_hash[:8]}... ({percentage:.2f}%) from device '{device}'")

    _database_service.save_kosync_document(kosync_doc)

    # Update linked book if exists
    linked_book = None
    if kosync_doc.linked_abs_id:
        linked_book = _database_service.get_book(kosync_doc.linked_abs_id)
    else:
        linked_book = _database_service.get_book_by_kosync_id(doc_hash)
        if linked_book:
            _database_service.link_kosync_document(doc_hash, linked_book.abs_id)

    # AUTO-DISCOVERY
    if not linked_book:
        auto_create = os.environ.get('AUTO_CREATE_EBOOK_MAPPING', 'true').lower() == 'true'

        if auto_create:
            if doc_hash not in _active_scans:
                _active_scans.add(doc_hash)

                def run_auto_discovery(doc_hash_val):
                    try:
                        logger.info(f"🔍 KOSync: Scheduled auto-discovery for unmapped document {doc_hash_val[:8]}...")
                        epub_filename = _try_find_epub_by_hash(doc_hash_val)

                        if epub_filename:
                            book_id = f"ebook-{doc_hash_val[:16]}"
                            book = Book(
                                abs_id=book_id,
                                abs_title=Path(epub_filename).stem,
                                ebook_filename=epub_filename,
                                kosync_doc_id=doc_hash_val,
                                transcript_file=None,
                                status='active',
                                duration=None,
                                sync_mode='ebook_only'
                            )
                            _database_service.save_book(book)
                            _database_service.link_kosync_document(doc_hash_val, book_id)
                            logger.info(f"✅ Auto-created ebook-only mapping: {book_id} -> {epub_filename}")

                            if _manager:
                                _manager.sync_cycle(target_abs_id=book_id)
                        else:
                            logger.debug(f"⚠️ Could not auto-match EPUB for KOSync document {doc_hash_val[:8]}...")
                    except Exception as e:
                        logger.error(f"Error in auto-discovery background task: {e}")
                    finally:
                        if doc_hash_val in _active_scans:
                            _active_scans.remove(doc_hash_val)

                threading.Thread(target=run_auto_discovery, args=(doc_hash,), daemon=True).start()

    if linked_book:
        state = State(
            abs_id=linked_book.abs_id,
            client_name='kosync',
            last_updated=time.time(),
            percentage=float(percentage),
            xpath=progress
        )
        _database_service.save_state(state)
        logger.debug(f"KOSync: Updated linked book '{linked_book.abs_title}' to {percentage:.2%}")

    return jsonify({
        "document": doc_hash,
        "timestamp": now.isoformat() + "Z"
    }), 200


# ---------------- Helper Functions ----------------

def _try_find_epub_by_hash(doc_hash: str) -> Optional[str]:
    """Try to find matching EPUB file for a KOSync document hash."""
    try:
        # Check hash cache first
        if _hash_cache:
            cached_filename = _hash_cache.lookup_by_hash(doc_hash)
            if cached_filename:
                try:
                    _container.ebook_parser().resolve_book_path(cached_filename)
                    logger.info(f"📚 Matched EPUB via hash cache: {cached_filename}")
                    return cached_filename
                except FileNotFoundError:
                    logger.debug(f"⚠️ Hash cache suggested '{cached_filename}' but file is missing. Re-scanning...")

        # Check filesystem
        if _ebook_dir and _ebook_dir.exists():
            logger.info(f"🔎 Starting filesystem search in {_ebook_dir} for hash {doc_hash[:8]}...")
            count = 0
            for epub_path in _ebook_dir.rglob("*.epub"):
                count += 1
                if count % 100 == 0:
                    logger.debug(f"Checked {count} local EPUBs...")

                if _hash_cache:
                    cached_hash = _hash_cache.lookup_by_filepath(epub_path)
                    if cached_hash:
                        if cached_hash == doc_hash:
                            logger.info(f"📚 Matched EPUB via filepath cache: {epub_path.name}")
                            return epub_path.name
                        continue

                try:
                    computed_hash = _container.ebook_parser().get_kosync_id(epub_path)

                    if _hash_cache:
                        _hash_cache.store_hash(computed_hash, epub_path.name, source='filesystem', filepath=epub_path)

                    if computed_hash == doc_hash:
                        logger.info(f"📚 Matched EPUB via filesystem: {epub_path.name}")
                        return epub_path.name
                except Exception as e:
                    logger.debug(f"Error checking file {epub_path.name}: {e}")
            logger.info(f"❌ Filesystem search finished. Checked {count} files. No match.")

        # Fallback to Booklore
        if _container.booklore_client().is_configured():
            logger.info("🔎 Starting Booklore API search...")

            try:
                books = _container.booklore_client().get_all_books()
                logger.info(f"Fetched {len(books)} books from Booklore. Scanning...")

                for book in books:
                    book_id = str(book['id'])

                    if _hash_cache:
                        cached_hash = _hash_cache.lookup_by_booklore_id(book_id)
                        if cached_hash:
                            if cached_hash == doc_hash:
                                safe_title = "".join(c for c in book['title'] if c.isalnum() or c in (' ', '-', '_')).rstrip() + ".epub"
                                try:
                                    _container.ebook_parser().resolve_book_path(safe_title)
                                    logger.info(f"📚 Matched EPUB via Booklore ID cache: {safe_title}")
                                    return safe_title
                                except FileNotFoundError:
                                    pass

                    try:
                        book_content = _container.booklore_client().download_book(book_id)
                        if book_content:
                            computed_hash = _container.ebook_parser().get_kosync_id_from_bytes(book['fileName'], book_content)

                            safe_title = "".join(c for c in book['title'] if c.isalnum() or c in (' ', '-', '_')).rstrip() + ".epub"

                            cache_dir = _container.data_dir() / "epub_cache"
                            cache_dir.mkdir(parents=True, exist_ok=True)
                            cache_path = cache_dir / safe_title
                            with open(cache_path, 'wb') as f:
                                f.write(book_content)
                            logger.info(f"📥 Persisted Booklore book to cache: {safe_title}")

                            if _hash_cache:
                                _hash_cache.store_hash(computed_hash, safe_title, source='booklore', booklore_id=book_id)

                            if computed_hash == doc_hash:
                                logger.info(f"📚 Matched EPUB via Booklore download: {safe_title}")
                                return safe_title
                    except Exception as e:
                        logger.warning(f"Failed to check Booklore book {book['title']}: {e}")

                logger.info(f"❌ Booklore search finished. Checked {len(books)} books. No match.")

            except Exception as e:
                logger.debug(f"Error querying Booklore for EPUB matching: {e}")

    except Exception as e:
        logger.error(f"Error in EPUB auto-discovery: {e}")

    logger.info("❌ Auto-discovery finished. No match found.")
    return None


# ---------------- KOSync Document Management API ----------------

@kosync_bp.route('/api/kosync-documents', methods=['GET'])
def api_get_kosync_documents():
    """Get all KOSync documents with their link status."""
    docs = _database_service.get_all_kosync_documents()
    result = []
    for doc in docs:
        linked_book = None
        if doc.linked_abs_id:
            linked_book = _database_service.get_book(doc.linked_abs_id)

        result.append({
            'document_hash': doc.document_hash,
            'progress': doc.progress,
            'percentage': float(doc.percentage) if doc.percentage else 0,
            'device': doc.device,
            'device_id': doc.device_id,
            'timestamp': doc.timestamp.isoformat() if doc.timestamp else None,
            'first_seen': doc.first_seen.isoformat() if doc.first_seen else None,
            'last_updated': doc.last_updated.isoformat() if doc.last_updated else None,
            'linked_abs_id': doc.linked_abs_id,
            'linked_book_title': linked_book.abs_title if linked_book else None
        })

    return jsonify({
        'documents': result,
        'total': len(result),
        'linked': sum(1 for d in result if d['linked_abs_id']),
        'unlinked': sum(1 for d in result if not d['linked_abs_id'])
    })


@kosync_bp.route('/api/kosync-documents/<doc_hash>/link', methods=['POST'])
def api_link_kosync_document(doc_hash):
    """Link a KOSync document to an ABS book."""
    data = request.json
    if not data or 'abs_id' not in data:
        return jsonify({'error': 'Missing abs_id'}), 400

    abs_id = data['abs_id']

    book = _database_service.get_book(abs_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    doc = _database_service.get_kosync_document(doc_hash)
    if not doc:
        return jsonify({'error': 'KOSync document not found'}), 404

    success = _database_service.link_kosync_document(doc_hash, abs_id)
    if success:
        if not book.kosync_doc_id:
            book.kosync_doc_id = doc_hash
            _database_service.save_book(book)

        return jsonify({'success': True, 'message': f'Linked to {book.abs_title}'})

    return jsonify({'error': 'Failed to link document'}), 500


@kosync_bp.route('/api/kosync-documents/<doc_hash>/unlink', methods=['POST'])
def api_unlink_kosync_document(doc_hash):
    """Remove the ABS book link from a KOSync document."""
    success = _database_service.unlink_kosync_document(doc_hash)
    if success:
        return jsonify({'success': True, 'message': 'Document unlinked'})
    return jsonify({'error': 'Document not found'}), 404


@kosync_bp.route('/api/kosync-documents/<doc_hash>', methods=['DELETE'])
def api_delete_kosync_document(doc_hash):
    """Delete a KOSync document."""
    success = _database_service.delete_kosync_document(doc_hash)
    if success:
        return jsonify({'success': True, 'message': 'Document deleted'})
    return jsonify({'error': 'Document not found'}), 404

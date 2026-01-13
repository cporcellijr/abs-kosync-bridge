#!/usr/bin/env python3
"""
Dependency Injection Container for abs-kosync-bridge.
Using python-dependency-injector library for proper DI functionality.
"""

import logging
from pathlib import Path
import os

from dependency_injector import containers, providers

# Import all the classes we'll be using
from src.api.api_clients import ABSClient, KoSyncClient
from src.api.booklore_client import BookloreClient
from src.api.hardcover_client import HardcoverClient
from src.db.json_db import JsonDB
from src.utils.ebook_utils import EbookParser
from src.utils.transcriber import AudioTranscriber
from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.sync_manager import SyncManager

logger = logging.getLogger(__name__)


def _create_storyteller_client():
    """Factory for creating Storyteller client with error handling."""
    StorytellerClientClass = None

    try:
        from src.api.storyteller_api import StorytellerDBWithAPI
        StorytellerClientClass = StorytellerDBWithAPI
    except ImportError:
        StorytellerClientClass = None

    if not StorytellerClientClass:
        try:
            from src.api.storyteller_db import StorytellerDB as StorytellerClientClass
        except ImportError:
            StorytellerClientClass = None

    if StorytellerClientClass:
        try:
            return StorytellerClientClass()
        except Exception as e:
            logger.error(f"⚠️ Failed to init Storyteller client: {e}. Using dummy implementation.")

    # Return dummy implementation
    class DummyStoryteller:
        def check_connection(self): return False

        def get_progress_with_fragment(self, *args): return None, None, None, None

        def update_progress(self, *args): return False

        def is_configured(self): return False

    return DummyStoryteller()


class Container(containers.DeclarativeContainer):
    """Main dependency injection container using dependency-injector library."""

    # Configuration
    config = providers.Configuration()

    # Configuration values from environment
    data_dir = providers.Object(Path(os.environ.get("DATA_DIR", "/data")))
    books_dir = providers.Object(Path(os.environ.get("BOOKS_DIR", "/books")))
    db_file = providers.Factory(
        lambda data_dir: data_dir / "mapping_db.json",
        data_dir=data_dir
    )
    state_file = providers.Factory(
        lambda data_dir: data_dir / "last_state.json",
        data_dir=data_dir
    )
    epub_cache_dir = providers.Factory(
        lambda data_dir: data_dir / "epub_cache",
        data_dir=data_dir
    )
    delta_abs_thresh = providers.Object(float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60)))
    delta_kosync_thresh = providers.Object(float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0)
    kosync_use_percentage_from_server = providers.Object(os.getenv("KOSYNC_USE_PERCENTAGE_FROM_SERVER", "false").lower() == "true")

    # API Clients
    abs_client = providers.Singleton(ABSClient)

    kosync_client = providers.Singleton(KoSyncClient)

    booklore_client = providers.Singleton(BookloreClient)

    hardcover_client = providers.Singleton(HardcoverClient)

    # Database handlers
    db_handler = providers.Singleton(
        JsonDB,
        db_file
    )

    state_handler = providers.Singleton(
        JsonDB,
        state_file
    )

    # Ebook parser
    ebook_parser = providers.Singleton(
        EbookParser,
        books_dir,
        epub_cache_dir=epub_cache_dir
    )

    # Storyteller client with factory
    storyteller_client = providers.Factory(
        _create_storyteller_client
    )

    # Transcriber
    transcriber = providers.Singleton(
        AudioTranscriber,
        data_dir
    )

    # Sync clients
    abs_sync_client = providers.Singleton(
        ABSSyncClient,
        abs_client,
        transcriber,
        ebook_parser,
        db_handler
    )

    kosync_sync_client = providers.Singleton(
        KoSyncSyncClient,
        kosync_client,
        ebook_parser
    )

    storyteller_sync_client = providers.Singleton(
        StorytellerSyncClient,
        storyteller_client,
        ebook_parser
    )

    booklore_sync_client = providers.Singleton(
        BookloreSyncClient,
        booklore_client,
        ebook_parser
    )

    abs_ebook_sync_client = providers.Singleton(
        ABSEbookSyncClient,
        abs_client,
        ebook_parser
    )

    # Sync clients dictionary for reuse
    sync_clients = providers.Dict(
        ABS=abs_sync_client,
        # TODO: needs further testing
        # "ABS eBook"=abs_ebook_sync_client,
        KoSync=kosync_sync_client,
        Storyteller=storyteller_sync_client,
        BookLore=booklore_sync_client
    )

    # Sync Manager
    sync_manager = providers.Singleton(
        SyncManager,
        abs_client=abs_client,
        kosync_client=kosync_client,
        hardcover_client=hardcover_client,
        storyteller_db=storyteller_client,
        booklore_client=booklore_client,
        transcriber=transcriber,
        ebook_parser=ebook_parser,
        db_handler=db_handler,
        state_handler=state_handler,
        sync_clients=sync_clients,
        kosync_use_percentage_from_server=kosync_use_percentage_from_server,
        epub_cache_dir=epub_cache_dir,
        data_dir=data_dir,
        books_dir=books_dir
    )


# Global container instance
container = Container()

def create_container() -> Container:
    """Create and configure the DI container with all application dependencies."""
    return container
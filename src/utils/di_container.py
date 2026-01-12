#!/usr/bin/env python3
"""
Dependency Injection Container for abs-kosync-bridge.
Provides Spring-like autowiring functionality for Python.
"""

import inspect
import logging
from typing import Type, TypeVar, Dict, Any, Callable
from pathlib import Path
import os
from src.utils.autowiring import autowire_constructor

logger = logging.getLogger(__name__)

T = TypeVar('T')


class DBHandler: pass
class StateHandler: pass
class StorytellerDBKey: pass
class TranscriberKey: pass


class DIContainer:
    """Dependency Injection Container with autowiring support."""

    def __init__(self):
        self._singletons: Dict[Type, Any] = {}
        self._factories: Dict[Type, Callable] = {}
        self._config_values: Dict[str, Any] = {}

    def register_singleton(self, interface: Type[T], implementation: Type[T] = None) -> None:
        """Register a class as a singleton. Implementation defaults to interface."""
        impl = implementation or interface
        self._singletons[interface] = impl

    def register_factory(self, interface: Type[T], factory: Callable[[], T]) -> None:
        """Register a factory function for creating instances."""
        self._factories[interface] = factory

    def register_value(self, name: str, value: Any) -> None:
        """Register a configuration value."""
        self._config_values[name] = value

    def get(self, interface):
        """Get an instance of the requested type, creating it if necessary."""
        # Check if already instantiated
        if interface in self._singletons and not inspect.isclass(self._singletons[interface]):
            return self._singletons[interface]

        # Check for factory
        if interface in self._factories:
            instance = self._factories[interface]()
            self._singletons[interface] = instance
            return instance

        # Get the class to instantiate
        impl_class = self._singletons.get(interface, interface)

        # Autowire dependencies
        instance = self._create_with_autowiring(impl_class)

        # Store as singleton if registered as such
        if interface in self._singletons:
            self._singletons[interface] = instance

        return instance

    def _create_with_autowiring(self, cls: Type[T]) -> T:
        """Create an instance with autowired dependencies."""
        return autowire_constructor(self, cls)

    def get_config_value(self, name: str):
        """Helper method to get config values."""
        return self._config_values.get(name)


def create_container() -> DIContainer:
    """Create and configure the DI container with all application dependencies."""
    container = DIContainer()

    # Configuration values from environment
    DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
    BOOKS_DIR = Path(os.environ.get("BOOKS_DIR", "/books"))

    container.register_value('data_dir', DATA_DIR)
    container.register_value('books_dir', BOOKS_DIR)
    container.register_value('db_file', DATA_DIR / "mapping_db.json")
    container.register_value('state_file', DATA_DIR / "last_state.json")
    container.register_value('epub_cache_dir', DATA_DIR / "epub_cache")
    container.register_value('delta_abs_thresh', float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60)))
    container.register_value('delta_kosync_thresh', float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0)
    container.register_value('kosync_use_percentage_from_server', os.getenv("KOSYNC_USE_PERCENTAGE_FROM_SERVER", "false").lower() == "true")

    # Register client singletons
    from src.api.api_clients import ABSClient, KoSyncClient
    from src.api.booklore_client import BookloreClient
    from src.api.hardcover_client import HardcoverClient
    from src.utils.ebook_utils import EbookParser
    from src.db.json_db import JsonDB

    container.register_singleton(ABSClient)
    container.register_singleton(KoSyncClient)
    container.register_singleton(BookloreClient)
    container.register_singleton(HardcoverClient)

    container.register_factory(EbookParser, lambda: EbookParser(
        container.get_config_value('books_dir'),
        epub_cache_dir=container.get_config_value('epub_cache_dir')
    ))

    container.register_factory(DBHandler, lambda: JsonDB(container.get_config_value('db_file')))
    container.register_factory(StateHandler, lambda: JsonDB(container.get_config_value('state_file')))
    container.register_factory(StorytellerDBKey, _create_storyteller_client)
    container.register_factory(TranscriberKey, lambda: _create_transcriber(container.get_config_value('data_dir')))

    from src.sync_clients.abs_sync_client import ABSSyncClient
    from src.sync_clients.kosync_sync_client import KoSyncSyncClient
    from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
    from src.sync_clients.booklore_sync_client import BookloreSyncClient
    from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient

    container.register_factory(ABSSyncClient, lambda: ABSSyncClient(
        container.get(ABSClient),
        container.get(TranscriberKey),
        container.get(EbookParser),
        container.get(DBHandler)
    ))

    container.register_factory(StorytellerSyncClient, lambda: StorytellerSyncClient(
        container.get(StorytellerDBKey),
        container.get(EbookParser)
    ))

    container.register_singleton(KoSyncSyncClient)
    container.register_singleton(ABSEbookSyncClient)
    container.register_singleton(BookloreSyncClient)

    # Register sync_clients dictionary for reuse
    container.register_factory('sync_clients', lambda: {
        "ABS": container.get(ABSSyncClient),
        # todo needs further testing
        # "ABS eBook": container.get(ABSEbookSyncClient),
        "KoSync": container.get(KoSyncSyncClient),
        "Storyteller": container.get(StorytellerSyncClient),
        "BookLore": container.get(BookloreSyncClient)
    })

    from src.sync_manager import SyncManager
    container.register_factory(SyncManager, lambda: SyncManager(
        abs_client=container.get(ABSClient),
        kosync_client=container.get(KoSyncClient),
        hardcover_client=container.get(HardcoverClient),
        storyteller_db=container.get(StorytellerDBKey),
        booklore_client=container.get(BookloreClient),
        transcriber=container.get(TranscriberKey),
        ebook_parser=container.get(EbookParser),
        db_handler=container.get(DBHandler),
        state_handler=container.get(StateHandler),
        sync_clients=container.get('sync_clients'),
        kosync_use_percentage_from_server=container.get_config_value('kosync_use_percentage_from_server'),
        epub_cache_dir=container.get_config_value('epub_cache_dir')
    ))

    return container


def _create_storyteller_client():
    """Factory for creating Storyteller client with error handling."""
    StorytellerClientClass = None

    try:
        from src.api.storyteller_api import StorytellerDBWithAPI
        StorytellerClientClass = StorytellerDBWithAPI
    except ImportError:
        pass

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


def _create_transcriber(data_dir):
    """Factory for creating transcriber with lazy loading."""
    from src.utils.transcriber import AudioTranscriber
    return AudioTranscriber(data_dir)

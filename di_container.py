#!/usr/bin/env python3
"""
Dependency Injection Container for abs-kosync-bridge.
Provides Spring-like autowiring functionality for Python.
"""

import inspect
import logging
from typing import Type, TypeVar, Dict, Any, Optional, Callable
from pathlib import Path
import os

logger = logging.getLogger(__name__)

T = TypeVar('T')


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
        # Handle string-based keys for factories
        if isinstance(interface, str):
            if interface in self._factories:
                if interface not in self._singletons or inspect.isclass(self._singletons.get(interface)):
                    instance = self._factories[interface]()
                    self._singletons[interface] = instance
                    return instance
                else:
                    return self._singletons[interface]
            else:
                raise ValueError(f"No factory registered for '{interface}'")

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
        from autowiring import autowire_constructor
        return autowire_constructor(self, cls)


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
    from api_clients import ABSClient, KoSyncClient
    from booklore_client import BookloreClient
    from hardcover_client import HardcoverClient
    from ebook_utils import EbookParser
    from json_db import JsonDB

    container.register_singleton(ABSClient)
    container.register_singleton(KoSyncClient)
    container.register_singleton(BookloreClient)
    container.register_singleton(HardcoverClient)

    # Register EbookParser with custom factory (needs special constructor args)
    container.register_factory(EbookParser, lambda: EbookParser(
        container.get_config_value('books_dir'),
        epub_cache_dir=container.get_config_value('epub_cache_dir')
    ))

    # Register JsonDB factories for different files
    container.register_factory('db_handler', lambda: JsonDB(container.get_config_value('db_file')))
    container.register_factory('state_handler', lambda: JsonDB(container.get_config_value('state_file')))

    # Register Storyteller with error handling
    container.register_factory('storyteller_db', _create_storyteller_client)

    # Register transcriber with lazy loading
    container.register_factory('transcriber', lambda: _create_transcriber(container.get_config_value('data_dir')))

    # Register sync clients with manual factory for ABSSyncClient and StorytellerSyncClient (need special handling)
    from src.sync_clients.abs_sync_client import ABSSyncClient
    from src.sync_clients.kosync_sync_client import KoSyncSyncClient
    from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
    from src.sync_clients.booklore_sync_client import BookloreSyncClient

    container.register_factory(ABSSyncClient, lambda: ABSSyncClient(
        container.get(ABSClient),
        container.get('transcriber'),
        container.get(EbookParser),
        container.get('db_handler')
    ))

    container.register_factory(StorytellerSyncClient, lambda: StorytellerSyncClient(
        container.get('storyteller_db'),
        container.get(EbookParser)
    ))

    container.register_singleton(KoSyncSyncClient)
    container.register_singleton(BookloreSyncClient)

    return container


def _create_storyteller_client():
    """Factory for creating Storyteller client with error handling."""
    StorytellerClientClass = None

    try:
        from storyteller_api import StorytellerDBWithAPI
        StorytellerClientClass = StorytellerDBWithAPI
    except ImportError:
        pass

    if not StorytellerClientClass:
        try:
            from storyteller_db import StorytellerDB as StorytellerClientClass
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
    from transcriber import AudioTranscriber
    return AudioTranscriber(data_dir)


# Extension methods for the container
def _get_config_value(self, name: str):
    """Helper method to get config values."""
    return self._config_values.get(name)

# Monkey patch the helper method
DIContainer.get_config_value = _get_config_value

"""
Database initialization and migration utilities.
This handles the one-time migration from JSON to SQLAlchemy on application startup.
"""

import logging
from pathlib import Path
from .database_service import DatabaseService, DatabaseMigrator

logger = logging.getLogger(__name__)


def initialize_database(data_dir: str = "data") -> DatabaseService:
    """
    Initialize the database and perform migration if needed.
    This should be called once on application startup.

    Args:
        data_dir: Directory containing the database and JSON files

    Returns:
        DatabaseService: Configured database service instance
    """
    data_path = Path(data_dir)
    db_path = data_path / "database.db"

    logger.info(f"Initializing database at {db_path}")

    # Create database service
    db_service = DatabaseService(str(db_path))

    # Check for migration
    json_mapping_path = data_path / "mapping_db.json"
    json_state_path = data_path / "last_state.json"

    migrator = DatabaseMigrator(
        db_service,
        str(json_mapping_path),
        str(json_state_path)
    )

    if migrator.should_migrate():
        logger.info("Performing one-time migration from JSON to SQLAlchemy...")
        migrator.migrate()
        logger.info("Migration completed successfully")
    else:
        logger.info("Database already initialized, no migration needed")

    return db_service


def get_database_service(data_dir: str = "data") -> DatabaseService:
    """
    Get a database service instance. Creates and migrates if needed.

    Args:
        data_dir: Directory containing the database

    Returns:
        DatabaseService: Configured database service instance
    """
    data_path = Path(data_dir)
    db_path = data_path / "database.db"

    return DatabaseService(str(db_path))

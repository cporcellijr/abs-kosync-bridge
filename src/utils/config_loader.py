
import os
import logging
from src.db.database_service import DatabaseService

logger = logging.getLogger(__name__)

class ConfigLoader:
    """
    Loads configuration from database and updates environment variables.
    Settings in the database take precedence over environment variables,
    except for critical paths that might be needed to connect to the DB itself
    (though currently DB path is hardcoded/passed in).
    """

    @staticmethod
    def load_settings(db_service: DatabaseService):
        """
        Load all settings from database and update os.environ.
        
        Args:
            db_service: Initialized DatabaseService instance
        """
        try:
            settings = db_service.get_all_settings()
            count = 0
            
            for key, value in settings.items():
                # Only apply if value is not None/Empty
                if value is not None and str(value).strip() != "":
                    # Update environment variable
                    os.environ[key] = str(value)
                    logger.debug(f"Loaded setting from DB: {key}={value if 'KEY' not in key and 'PASSWORD' not in key and 'TOKEN' not in key else '******'}")
                    count += 1
            
            logger.info(f"⚙️  Loaded {count} settings from database")
            
        except Exception as e:
            logger.error(f"⚠️  Error loading settings from database: {e}")
            # Do not re-raise, fall back to existing env vars


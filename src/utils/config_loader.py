
import os
import logging
from src.db.database_service import DatabaseService

logger = logging.getLogger(__name__)

# Full list of settings to manage
ALL_SETTINGS = [
    # Required ABS
    'ABS_SERVER', 'ABS_KEY', 'ABS_LIBRARY_ID',
    
    # Optional ABS
    'ABS_COLLECTION_NAME', 'ABS_PROGRESS_OFFSET_SECONDS', 'ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID',
    
    # KOSync
    'KOSYNC_ENABLED', 'KOSYNC_SERVER', 'KOSYNC_USER', 'KOSYNC_KEY', 
    'KOSYNC_HASH_METHOD', 'KOSYNC_USE_PERCENTAGE_FROM_SERVER',
    
    # Storyteller
    'STORYTELLER_ENABLED', 'STORYTELLER_API_URL', 'STORYTELLER_USER', 'STORYTELLER_PASSWORD',
    
    # Booklore
    'BOOKLORE_ENABLED', 'BOOKLORE_SERVER', 'BOOKLORE_USER', 'BOOKLORE_PASSWORD', 'BOOKLORE_SHELF_NAME',
    
    # Hardcover
    'HARDCOVER_ENABLED', 'HARDCOVER_TOKEN',
    
    # Telegram
    'TELEGRAM_ENABLED', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'TELEGRAM_LOG_LEVEL',
    
    # Shelfmark
    'SHELFMARK_URL',
    
    # Sync Behavior
    'SYNC_PERIOD_MINS', 'SYNC_DELTA_ABS_SECONDS', 'SYNC_DELTA_KOSYNC_PERCENT', 
    'SYNC_DELTA_BETWEEN_CLIENTS_PERCENT', 'SYNC_DELTA_KOSYNC_WORDS',
    'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT', 'SYNC_ABS_EBOOK',
    'FUZZY_MATCH_THRESHOLD', 'SUGGESTIONS_ENABLED',
    
    # System
    'TZ', 'LOG_LEVEL', 'DATA_DIR', 'BOOKS_DIR', 'LINKER_BOOKS_DIR', 'PROCESSING_DIR', 
    'STORYTELLER_INGEST_DIR', 'AUDIOBOOKS_DIR', 'EBOOK_CACHE_SIZE',
    'JOB_MAX_RETRIES', 'JOB_RETRY_DELAY_MINS', 'MONITOR_INTERVAL', 'WHISPER_MODEL',
    'WHISPER_DEVICE', 'WHISPER_COMPUTE_TYPE',
    'TRANSCRIPTION_PROVIDER', 'DEEPGRAM_API_KEY', 'DEEPGRAM_MODEL'
]

# Default values
DEFAULT_CONFIG = {
    'TZ': 'America/New_York',
    'LOG_LEVEL': 'INFO',
    'DATA_DIR': '/data',
    'BOOKS_DIR': '/books',
    'ABS_COLLECTION_NAME': 'Synced with KOReader',
    'BOOKLORE_SHELF_NAME': 'Kobo',
    'SYNC_PERIOD_MINS': '5',
    'SYNC_DELTA_ABS_SECONDS': '60',
    'SYNC_DELTA_KOSYNC_PERCENT': '0.5',
    'SYNC_DELTA_BETWEEN_CLIENTS_PERCENT': '0.5',
    'SYNC_DELTA_KOSYNC_WORDS': '400',
    'FUZZY_MATCH_THRESHOLD': '80',
    'WHISPER_MODEL': 'tiny',
    'WHISPER_DEVICE': 'auto',
    'WHISPER_COMPUTE_TYPE': 'auto',
    'TRANSCRIPTION_PROVIDER': 'local',
    'DEEPGRAM_API_KEY': '',
    'DEEPGRAM_MODEL': 'nova-2',
    'JOB_MAX_RETRIES': '5',
    'JOB_RETRY_DELAY_MINS': '15',
    'MONITOR_INTERVAL': '3600',
    'LINKER_BOOKS_DIR': '/linker_books',
    'PROCESSING_DIR': '/processing',
    'STORYTELLER_INGEST_DIR': '/linker_books',
    'AUDIOBOOKS_DIR': '/audiobooks',
    'ABS_PROGRESS_OFFSET_SECONDS': '0',
    'EBOOK_CACHE_SIZE': '3',
    'KOSYNC_HASH_METHOD': 'content',
    'TELEGRAM_LOG_LEVEL': 'ERROR',
    'SHELFMARK_URL': '',
    'KOSYNC_ENABLED': 'false',
    'STORYTELLER_ENABLED': 'false',
    'BOOKLORE_ENABLED': 'false',
    'HARDCOVER_ENABLED': 'false',
    'TELEGRAM_ENABLED': 'false',
    'SUGGESTIONS_ENABLED': 'false',
    'KOSYNC_USE_PERCENTAGE_FROM_SERVER': 'false',
    'SYNC_ABS_EBOOK': 'false',
    'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT': 'false',
    'ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID': 'false'
}

class ConfigLoader:
    """
    Loads configuration from database and updates environment variables.
    Settings in the database take precedence over environment variables,
    except for critical paths that might be needed to connect to the DB itself.
    """

    @staticmethod
    def bootstrap_config(db_service: DatabaseService):
        """
        If settings table is empty, populate it from os.environ or defaults.
        This provides a smooth migration for existing users.
        """
        try:
            # Check if we have any settings
            existing_settings = db_service.get_all_settings()
            if existing_settings:
                # Already bootstrapped
                return

            logger.info("🚀 Bootstrapping configuration from environment variables...")
            
            count = 0
            for key in ALL_SETTINGS:
                # Priority: 1. Env Var, 2. Default, 3. Empty string
                val = os.environ.get(key, DEFAULT_CONFIG.get(key, ""))
                
                # Check for None explicitly
                if val is None:
                    val = ""
                
                db_service.set_setting(key, str(val))
                count += 1
            
            logger.info(f"✅ Bootstrapped {count} settings to database")

        except Exception as e:
            logger.error(f"⚠️  Error bootstrapping config: {e}")

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
                # Apply validation or type conversion if needed (mostly string for env vars)
                val_str = str(value) if value is not None else ""
                
                # Update environment variable
                os.environ[key] = val_str
                
                # Mask secrets in logs
                log_val = "******" if any(s in key for s in ['KEY', 'PASSWORD', 'TOKEN']) else val_str
                # logger.debug(f"Loaded {key}={log_val}")
                count += 1
            
            logger.info(f"⚙️  Loaded {count} settings from database")
            
        except Exception as e:
            logger.error(f"⚠️  Error loading settings from database: {e}")
            # Do not re-raise, fall back to existing env vars

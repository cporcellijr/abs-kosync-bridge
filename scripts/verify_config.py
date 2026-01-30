
import os
import sys
import logging

# Add src to path
sys.path.append(os.getcwd())

from src.db.migration_utils import get_database_service
from src.utils.config_loader import ConfigLoader, ALL_SETTINGS

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Verification")

def verify_config_system():
    logger.info("🧪 Starting Config System Verification")
    
    # 1. Initialize DB Service
    # Use a test DB file to avoid messing with real data? 
    # Or just use the real one since we backed it up and bootstrap shouldn't overwrite existing?
    # Let's use a temp db for safety.
    
    TEST_DB_DIR = "test_data"
    os.makedirs(TEST_DB_DIR, exist_ok=True)
    db_path = os.path.join(TEST_DB_DIR, "database.db")
    if os.path.exists(db_path):
        os.remove(db_path)
        
    db_service = get_database_service(TEST_DB_DIR)
    
    # 2. Mock Environment
    os.environ["TEST_SETTING_1"] = "ValueFromEnv"
    # Ensure our ConfigLoader knows about this key (it won't unless we hack ALL_SETTINGS or use known keys)
    # Let's use a real key from ALL_SETTINGS
    os.environ["TZ"] = "Europe/London" # Different from default
    
    # 3. Bootstrap
    logger.info("Running bootstrap_config...")
    ConfigLoader.bootstrap_config(db_service)
    
    # 4. Verify DB content
    settings = db_service.get_all_settings()
    if settings.get("TZ") == "Europe/London":
        logger.info("✅ Bootstrap correctly picked up TZ from Env")
    else:
        logger.error(f"❌ Bootstrap failed to pick up TZ. Got: {settings.get('TZ')}")
        
    if settings.get("LOG_LEVEL") == "INFO": # Default
        logger.info("✅ Bootstrap correctly used default for LOG_LEVEL")
    else:
         logger.error(f"❌ Bootstrap failed default check. Got: {settings.get('LOG_LEVEL')}")

    # 5. Modify DB
    logger.info("Modifying DB setting 'LOG_LEVEL' to 'DEBUG'...")
    db_service.set_setting("LOG_LEVEL", "DEBUG")
    
    # 6. Load Settings
    logger.info("Running load_settings...")
    ConfigLoader.load_settings(db_service)
    
    # 7. Verify Env Update
    if os.environ.get("LOG_LEVEL") == "DEBUG":
        logger.info("✅ load_settings correctly updated os.environ")
    else:
        logger.error(f"❌ load_settings failed. os.environ['LOG_LEVEL'] = {os.environ.get('LOG_LEVEL')}")

    logger.info("🎉 Verification Complete")

if __name__ == "__main__":
    verify_config_system()

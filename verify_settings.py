
import os
import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path.cwd()))

from src.db.migration_utils import initialize_database
from src.utils.config_loader import ConfigLoader


def test_settings_flow():
    print("Starting Settings Verification...")
    
    # 1. Initialize DB (run migrations)
    print("\n[1/4] Initializing Database & Migrations...")
    db_service = initialize_database(data_dir="data")
    
    # 2. Test Setting & Getting
    print("\n[2/4] Testing CRUD Operations...")
    test_key = "TEST_SETTING_KEY"
    test_val = "test_value_123"
    
    # Set
    db_service.set_setting(test_key, test_val)
    print(f"   -> Set {test_key} = {test_val}")
    
    # Get
    retrieved = db_service.get_setting(test_key)
    print(f"   -> Retrieved: {retrieved}")
    
    if retrieved != test_val:
        print("   FAIL: Value mismatch")
        return False
    else:
        print("   PASS: Value matches")

    # 3. Test Config Loader (Env injection)
    print("\n[3/4] Testing Config Loader...")
    ConfigLoader.load_settings(db_service)
    
    env_val = os.environ.get(test_key)
    print(f"   -> os.environ['{test_key}'] = {env_val}")
    
    if env_val != test_val:
         print("   FAIL: Env var not updated")
         return False
    else:
         print("   PASS: Env var updated")

    # 4. Clean up
    print("\n[4/4] Cleaning up...")
    db_service.delete_setting(test_key)
    retrieved_after_delete = db_service.get_setting(test_key)
    if retrieved_after_delete is None:
        print("   PASS: Setting deleted")
    else:
        print("   FAIL: Setting not deleted")
        return False

    print("\nVerification Complete!")
    return True

if __name__ == "__main__":
    test_settings_flow()

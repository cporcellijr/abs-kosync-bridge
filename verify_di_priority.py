
import os
import sys
from pathlib import Path
from unittest.mock import patch

# Add src to path
sys.path.append(str(Path.cwd()))

def test_di_priority():
    print("Testing DI Container Priority...")
    
    # 1. Set initial ENV var
    os.environ["DATA_DIR"] = "/initial/data"
    
    # 2. Import Container (factory should NOT evaluate yet)
    from src.utils.di_container import create_container
    container = create_container()
    
    # 3. Verify accessing it gets the initial value
    print(f"Initial Container Data Dir: {container.data_dir()}")
    if str(container.data_dir()) != str(Path("/initial/data")):
        print(f"FAIL: Expected /initial/data, got {container.data_dir()}")
        return False
        
    # 4. Update ENV var (Simulating ConfigLoader)
    print("Updating OS Environ...")
    os.environ["DATA_DIR"] = "/updated/data"
    
    # 5. Verify accessing it AGAIN gets the NEW value (Lazy Evaluation)
    # Since we changed providers to Factory/lambda, it should re-evaluate
    print(f"Updated Container Data Dir: {container.data_dir()}")
    
    if str(container.data_dir()) == str(Path("/updated/data")):
        print("PASS: Container picked up new env var!")
        return True
    else:
        print(f"FAIL: Container stuck on old value. Got {container.data_dir()}")
        return False

if __name__ == "__main__":
    success = test_di_priority()
    if not success:
        sys.exit(1)

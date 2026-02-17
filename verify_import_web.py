
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

try:
    from src.web_server import create_app
    print("Import successful")
except Exception as e:
    print(f"Import failed: {e}")
    import traceback
    traceback.print_exc()

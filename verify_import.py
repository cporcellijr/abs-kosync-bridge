
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

try:
    from src.services.forge_service import ForgeService
    print("Import successful")
except Exception as e:
    print(f"Import failed: {e}")
    import traceback
    traceback.print_exc()

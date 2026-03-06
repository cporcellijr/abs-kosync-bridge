import html
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def safe_folder_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    name = html.escape(str(name or '').strip())[:150]
    for c in invalid:
        name = name.replace(c, '_')
    return name.strip() or "Unknown"


def atomic_stage_folder(source_dir: Path, final_dir: Path) -> Optional[Path]:
    source_dir = Path(source_dir)
    final_dir = Path(final_dir)
    hidden_dir = final_dir.parent / f".staging_{final_dir.name}"

    try:
        if hidden_dir.exists():
            shutil.rmtree(hidden_dir)
        if final_dir.exists():
            shutil.rmtree(final_dir)

        shutil.move(str(source_dir), str(hidden_dir))
        hidden_dir.rename(final_dir)
        return final_dir
    except Exception as stage_err:
        logger.error(f"❌ Storyteller atomic staging failed: {stage_err}")
        try:
            if source_dir.exists():
                shutil.rmtree(source_dir)
        except Exception:
            pass
        try:
            if hidden_dir.exists():
                shutil.rmtree(hidden_dir)
        except Exception:
            pass
        return None


def stage_readaloud_to_storyteller(
    readaloud_path: Path,
    title: str,
    abs_id: str,
    storyteller_lib_dir: Path,
) -> Optional[Path]:
    readaloud_path = Path(readaloud_path)
    storyteller_lib_dir = Path(storyteller_lib_dir)
    if not readaloud_path.exists() or not storyteller_lib_dir.exists():
        return None

    safe_title = safe_folder_name(title or abs_id)
    final_dir = storyteller_lib_dir / safe_title

    with tempfile.TemporaryDirectory(prefix=f"sync_readaloud_{abs_id}_") as tmp_dir:
        staging_dir = Path(tmp_dir) / safe_title
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged_epub = staging_dir / f"{safe_title}.epub"
        shutil.copy2(readaloud_path, staged_epub)
        return atomic_stage_folder(staging_dir, final_dir)

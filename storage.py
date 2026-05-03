"""storage.py — load/save PawPal state to a JSON file. Pure I/O; no business logic."""

import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

_EMPTY: dict[str, list[dict]] = {"pets": [], "tasks": []}


def load_state(path: str) -> dict[str, list[dict]]:
    """Read pets and tasks from *path*.

    Returns:
        Dict with keys "pets" and "tasks" (each a list of serialized dicts).
        Returns empty state if the file does not exist or cannot be parsed.
    """
    if not os.path.exists(path):
        return {"pets": [], "tasks": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("Storage file %r has unexpected format — returning empty state.", path)
            return {"pets": [], "tasks": []}
        pets = data.get("pets", [])
        tasks = data.get("tasks", [])
        if not isinstance(pets, list):
            logger.warning(
                "Storage file %r: 'pets' expected list, got %s — returning empty state.",
                path, type(pets).__name__,
            )
            return {"pets": [], "tasks": []}
        if not isinstance(tasks, list):
            logger.warning(
                "Storage file %r: 'tasks' expected list, got %s — returning empty state.",
                path, type(tasks).__name__,
            )
            return {"pets": [], "tasks": []}
        return {"pets": list(pets), "tasks": list(tasks)}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Cannot read storage file %r (%s) — returning empty state.", path, exc)
        return {"pets": [], "tasks": []}


def save_state(path: str, pets: list[dict], tasks: list[dict]) -> None:
    """Write pets and tasks to *path* using an atomic temp-file replace.

    Strategy:
    1. Normalize *path* to an absolute path and create parent directories.
    2. Write JSON to a sibling .tmp file, flush, and fsync for durability.
    3. Atomically replace the target with os.replace().
    4. Windows fallback: if os.replace() raises PermissionError (winerror 5),
       the file is likely open in another process. Log a warning, remove the
       temp file, and direct-write to *path* instead (best-effort).
    5. A ``finally`` block ensures the temp file is never left on disk.

    Raises:
        OSError: if both the atomic replace and the fallback write fail.
    """
    data     = {"pets": pets, "tasks": tasks}
    abs_path = os.path.abspath(path)
    dir_name = os.path.dirname(abs_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # fsync is best-effort on some filesystems
        try:
            os.replace(tmp_path, abs_path)
            tmp_path = None  # atomic replace succeeded; nothing left to clean up
        except PermissionError as exc:
            # Windows: target file is held open by another process (winerror 5).
            # Fall back to a direct write so data is not lost.
            _winerr = getattr(exc, "winerror", None)
            if _winerr not in (None, 5):
                raise
            logger.warning(
                "Atomic replace blocked on %r (winerror=%s) — "
                "falling back to direct write. "
                "Close pawpal.json in any editor and stop duplicate Streamlit runs.",
                abs_path, _winerr,
            )
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            tmp_path = None  # temp file removed; skip finally cleanup
            with open(abs_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
    except OSError as exc:
        logger.error("Failed to save state to %r: %s", abs_path, exc)
        raise
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

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

    Writing to a temp file first ensures a crash mid-write never corrupts data.

    Raises:
        OSError: if the write or rename fails.
    """
    data     = {"pets": pets, "tasks": tasks}
    dir_name = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.error("Failed to save state to %r: %s", path, exc)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

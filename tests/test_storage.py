"""Tests for storage.py (load/save) and PawPalSystem persistence wiring."""

import json
import os
import pytest
from datetime import datetime

from storage import load_state, save_state
from pawpal_system import (
    FeedingTask, MedicationTask, Pet, PawPalSystem, TaskStatus,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pet(**kwargs) -> Pet:
    defaults = {"name": "Rex", "species": "dog", "breed": "Lab", "weight": 25.0}
    return Pet(**{**defaults, **kwargs})

def _task(pet_id: str, **kwargs):
    defaults = {
        "pet_id":   pet_id,
        "due_at":   datetime(2026, 5, 3, 8, 0),
        "priority": 1,
        "metadata": {"medication_name": "Heartguard"},
    }
    return MedicationTask(**{**defaults, **kwargs})


# ── storage.load_state ────────────────────────────────────────────────────────

def test_load_missing_file_returns_empty(tmp_path):
    path = str(tmp_path / "nonexistent.json")
    state = load_state(path)
    assert state == {"pets": [], "tasks": []}


def test_load_corrupt_json_returns_empty(tmp_path):
    path = str(tmp_path / "bad.json")
    with open(path, "w") as f:
        f.write("not json at all ~~~~")
    state = load_state(path)
    assert state == {"pets": [], "tasks": []}


def test_load_wrong_top_level_type_returns_empty(tmp_path):
    """A file whose top-level JSON value is a list (not a dict) returns empty state."""
    path = str(tmp_path / "list.json")
    with open(path, "w") as f:
        json.dump([1, 2, 3], f)
    state = load_state(path)
    assert state == {"pets": [], "tasks": []}


def test_load_missing_keys_returns_empty_lists(tmp_path):
    """A valid JSON object that lacks pets/tasks keys returns empty lists, not an error."""
    path = str(tmp_path / "partial.json")
    with open(path, "w") as f:
        json.dump({"other_key": "value"}, f)
    state = load_state(path)
    assert state["pets"]  == []
    assert state["tasks"] == []


def test_load_pets_is_dict_returns_empty(tmp_path):
    """When 'pets' is a dict (not a list), load_state returns empty state."""
    path = str(tmp_path / "bad_pets.json")
    with open(path, "w") as f:
        json.dump({"pets": {"id": "abc"}, "tasks": []}, f)
    state = load_state(path)
    assert state == {"pets": [], "tasks": []}


def test_load_tasks_is_string_returns_empty(tmp_path):
    """When 'tasks' is a string (not a list), load_state returns empty state."""
    path = str(tmp_path / "bad_tasks.json")
    with open(path, "w") as f:
        json.dump({"pets": [], "tasks": "oops"}, f)
    state = load_state(path)
    assert state == {"pets": [], "tasks": []}


# ── storage.save_state / round-trip ──────────────────────────────────────────

def test_save_creates_file(tmp_path):
    path = str(tmp_path / "db.json")
    save_state(path, [], [])
    assert os.path.exists(path)


def test_save_then_load_round_trip_pets(tmp_path):
    path = str(tmp_path / "db.json")
    pet  = _pet(name="Buddy")
    save_state(path, [pet.to_dict()], [])
    state = load_state(path)
    assert len(state["pets"]) == 1
    assert state["pets"][0]["name"] == "Buddy"
    assert state["pets"][0]["id"]   == pet.id


def test_save_then_load_round_trip_tasks(tmp_path):
    path  = str(tmp_path / "db.json")
    pet   = _pet()
    task  = _task(pet.id)
    save_state(path, [pet.to_dict()], [task.to_dict()])
    state = load_state(path)
    assert len(state["tasks"]) == 1
    assert state["tasks"][0]["id"]     == task.id
    assert state["tasks"][0]["pet_id"] == pet.id
    assert state["tasks"][0]["type"]   == "medication"


def test_atomic_write_no_tmp_file_left(tmp_path):
    """After a successful save, no .tmp files should remain in the directory."""
    path = str(tmp_path / "db.json")
    save_state(path, [], [])
    leftover = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftover == []


# ── PawPalSystem persistence wiring ──────────────────────────────────────────

def test_system_starts_empty_when_file_missing(tmp_path):
    path   = str(tmp_path / "db.json")
    system = PawPalSystem(db_path=path)
    assert system.get_pets()  == []
    assert system.get_tasks() == []


def test_system_persists_add_pet(tmp_path):
    path = str(tmp_path / "db.json")
    s1   = PawPalSystem(db_path=path)
    pet  = s1.add_pet(_pet(name="Fido"))

    s2 = PawPalSystem(db_path=path)
    pets = s2.get_pets()
    assert len(pets) == 1
    assert pets[0].name == "Fido"
    assert pets[0].id   == pet.id


def test_system_persists_add_task(tmp_path):
    path   = str(tmp_path / "db.json")
    system = PawPalSystem(db_path=path)
    pet    = system.add_pet(_pet())
    task   = system.add_task(_task(pet.id))

    reload = PawPalSystem(db_path=path)
    tasks  = reload.get_tasks()
    assert len(tasks) == 1
    assert tasks[0].id     == task.id
    assert tasks[0].pet_id == pet.id


def test_system_persists_mark_complete(tmp_path):
    path   = str(tmp_path / "db.json")
    system = PawPalSystem(db_path=path)
    pet    = system.add_pet(_pet())
    task   = system.add_task(_task(pet.id))
    system.mark_complete(task.id)

    reload = PawPalSystem(db_path=path)
    assert reload.get_tasks()[0].status == TaskStatus.COMPLETE


def test_system_reloads_multiple_pets_and_tasks(tmp_path):
    path   = str(tmp_path / "db.json")
    system = PawPalSystem(db_path=path)
    p1 = system.add_pet(_pet(name="Rex"))
    p2 = system.add_pet(_pet(name="Luna"))
    system.add_task(_task(p1.id))
    system.add_task(_task(p2.id))

    reload = PawPalSystem(db_path=path)
    assert len(reload.get_pets())  == 2
    assert len(reload.get_tasks()) == 2
    assert {p.name for p in reload.get_pets()} == {"Rex", "Luna"}


def test_system_corrupt_file_starts_empty(tmp_path):
    """A corrupt DB file should not crash PawPalSystem — it starts with empty state."""
    path = str(tmp_path / "db.json")
    with open(path, "w") as f:
        f.write("corrupted ~~~~")

    system = PawPalSystem(db_path=path)
    assert system.get_pets()  == []
    assert system.get_tasks() == []

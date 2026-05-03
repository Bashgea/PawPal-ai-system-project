"""pawpal_system.py — all backend classes for PawPal+."""

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────

class TaskValidationError(Exception):
    """A task failed validation."""

class SchedulingConflict(Exception):
    """Two tasks conflict in the schedule."""


# ── Status ────────────────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    PENDING  = "pending"
    COMPLETE = "complete"
    SKIPPED  = "skipped"


# ── Pet ───────────────────────────────────────────────────────────────────────

@dataclass
class Pet:
    name:    str
    species: str
    breed:   str
    weight:  float
    notes:   str = ""
    id:      str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        return {
            "id":      self.id,
            "name":    self.name,
            "species": self.species,
            "breed":   self.breed,
            "weight":  self.weight,
            "notes":   self.notes,
        }

    @staticmethod
    def from_dict(data: dict) -> "Pet":
        return Pet(
            name    = data["name"],
            species = data["species"],
            breed   = data["breed"],
            weight  = float(data["weight"]),
            notes   = data.get("notes", ""),
            id      = data.get("id", str(uuid.uuid4())),
        )


# ── Task (abstract base) ──────────────────────────────────────────────────────

@dataclass
class Task(ABC):
    """Base class for all pet-care tasks. Subclasses fill in urgency and validation."""

    pet_id:   str
    due_at:   datetime
    priority: int
    status:   TaskStatus       = TaskStatus.PENDING
    metadata: dict[str, Any]   = field(default_factory=dict)
    id:       str               = field(default_factory=lambda: str(uuid.uuid4()))

    @abstractmethod
    def urgency_score(self, now: datetime) -> float:
        """Higher number = more urgent. Overdue tasks return 10+."""

    @abstractmethod
    def validate(self) -> list[str]:
        """Return a list of problems. Empty list means the task is valid."""

    def to_dict(self) -> dict:
        task_type = self.__class__.__name__.lower().replace("task", "")
        return {
            "type":     task_type,
            "id":       self.id,
            "pet_id":   self.pet_id,
            "due_at":   self.due_at.isoformat(),
            "priority": self.priority,
            "status":   self.status.value,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        """Build the right Task subclass from a saved dict."""
        type_map = {
            "feeding":     FeedingTask,
            "walk":        WalkTask,
            "medication":  MedicationTask,
            "appointment": AppointmentTask,
        }
        klass = type_map.get(data.get("type", ""))
        if klass is None:
            raise TaskValidationError(f"Unknown task type: {data.get('type')!r}")
        return klass(
            pet_id   = data["pet_id"],
            due_at   = datetime.fromisoformat(data["due_at"]),
            priority = int(data.get("priority", 0)),
            status   = TaskStatus(data.get("status", TaskStatus.PENDING.value)),
            metadata = data.get("metadata", {}),
            id       = data.get("id", str(uuid.uuid4())),
        )


# ── Task subclasses ───────────────────────────────────────────────────────────

@dataclass
class FeedingTask(Task):
    """A scheduled feeding. Optional metadata: portion_size (number)."""

    def urgency_score(self, now: datetime) -> float:
        minutes_left = (self.due_at - now).total_seconds() / 60
        if minutes_left < 0:
            return 10 + abs(minutes_left) / 60   # overdue — grows every hour
        if minutes_left < 60:
            return 5                               # due within the hour
        if minutes_left < 240:
            return 2                               # due within 4 hours
        return 0

    def validate(self) -> list[str]:
        problems = []
        portion = self.metadata.get("portion_size")
        if portion is not None and float(portion) <= 0:
            problems.append("portion_size must be greater than 0")
        return problems


@dataclass
class WalkTask(Task):
    """A scheduled walk. Optional metadata: duration_minutes (number)."""

    def urgency_score(self, now: datetime) -> float:
        minutes_left = (self.due_at - now).total_seconds() / 60
        if minutes_left < 0:
            return 8 + abs(minutes_left) / 60
        if minutes_left < 60:
            return 4
        if minutes_left < 180:
            return 2
        return 0

    def validate(self) -> list[str]:
        problems = []
        duration = self.metadata.get("duration_minutes")
        if duration is not None and float(duration) <= 0:
            problems.append("duration_minutes must be greater than 0")
        return problems


@dataclass
class MedicationTask(Task):
    """A scheduled medication dose. Required metadata: medication_name (str)."""

    def urgency_score(self, now: datetime) -> float:
        # Medication is always the top priority.
        minutes_left = (self.due_at - now).total_seconds() / 60
        if minutes_left < 0:
            return 100 + abs(minutes_left) / 60
        if minutes_left < 30:
            return 20
        if minutes_left < 60:
            return 10
        return 0

    def validate(self) -> list[str]:
        problems = []
        if not self.metadata.get("medication_name"):
            problems.append("medication_name is required in metadata")
        dose = self.metadata.get("dose")
        if dose is not None and float(dose) <= 0:
            problems.append("dose must be greater than 0")
        return problems


@dataclass
class AppointmentTask(Task):
    """A vet or grooming appointment. Required metadata: location or vet_name."""

    def urgency_score(self, now: datetime) -> float:
        minutes_left = (self.due_at - now).total_seconds() / 60
        if minutes_left < 0:
            return 50 + abs(minutes_left) / 60
        if minutes_left < 60:
            return 15
        if minutes_left < 120:
            return 5
        return 0

    def validate(self) -> list[str]:
        problems = []
        has_location = self.metadata.get("location") or self.metadata.get("vet_name")
        if not has_location:
            problems.append("Either location or vet_name is required in metadata")
        return problems


# ── Schedule ──────────────────────────────────────────────────────────────────

@dataclass
class Schedule:
    """A list of tasks for a given time window."""

    tasks:        list[Task]
    window_start: datetime
    window_end:   datetime

    def filter(self, fn) -> "Schedule":
        """Return a new Schedule with only the tasks where fn(task) is True."""
        return Schedule(
            tasks        = [t for t in self.tasks if fn(t)],
            window_start = self.window_start,
            window_end   = self.window_end,
        )

    def sort_by_urgency(self, now: datetime) -> "Schedule":
        """Return a new Schedule sorted from most to least urgent."""
        sorted_tasks = sorted(self.tasks, key=lambda t: t.urgency_score(now), reverse=True)
        return Schedule(sorted_tasks, self.window_start, self.window_end)

    def detect_conflicts(self) -> list[str]:
        """Return a list of conflict descriptions (same pet, same time)."""
        seen = {}
        conflicts = []
        for task in self.tasks:
            key = (task.pet_id, task.due_at)
            if key in seen:
                conflicts.append(
                    f"Tasks {seen[key]!r} and {task.id!r} are both "
                    f"scheduled at {task.due_at.isoformat()}"
                )
            else:
                seen[key] = task.id
        return conflicts


# ── Scheduler ─────────────────────────────────────────────────────────────────

class Scheduler:
    """Builds the daily schedule. Pure — no I/O, no side effects."""

    def build(self, pets: list[Pet], tasks: list[Task], now: datetime) -> Schedule:
        """Return today's tasks sorted by urgency."""
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)

        # Grab pending tasks due today (includes anything overdue from past days).
        todays_tasks = [
            t for t in tasks
            if t.status == TaskStatus.PENDING and t.due_at < day_end
        ]

        schedule = Schedule(todays_tasks, day_start, day_end)
        schedule = self.apply_rules(schedule, now)
        return schedule.sort_by_urgency(now)

    def apply_rules(self, schedule: Schedule, now: datetime) -> Schedule:
        """Apply scheduling rules (walk cooldowns, medication spacing, etc.).
        Add new rules here as pure functions — each takes a Schedule and returns one.
        """
        return schedule


# ── PlanResult ────────────────────────────────────────────────────────────────

@dataclass
class PlanResult:
    """What the AI agent (or fallback) returns after planning the day."""

    ordered_tasks: list[Task]
    time_windows:  dict[str, str]
    rationales:    list[str]
    flagged_risks: list[str]
    is_ai_planned: bool = False


# ── PawPalSystem (Facade) ─────────────────────────────────────────────────────

class PawPalSystem:
    """The main entry point. UI, demo, and agent all talk only to this class."""

    def __init__(self):
        self._pets: dict[str, Pet]   = {}
        self._tasks: dict[str, Task] = {}
        self._scheduler = Scheduler()

    # -- Pets --

    def add_pet(self, pet: Pet) -> Pet:
        """Add a pet. Generates an id if one wasn't provided."""
        if not pet.id:
            pet.id = str(uuid.uuid4())
        self._pets[pet.id] = pet
        logger.info("Pet added: %s (%s)", pet.name, pet.id)
        return pet

    def get_pets(self) -> list[Pet]:
        return list(self._pets.values())

    def get_pet(self, pet_id: str) -> Pet:
        if pet_id not in self._pets:
            raise KeyError(f"No pet found with id {pet_id!r}")
        return self._pets[pet_id]

    # -- Tasks --

    def add_task(self, task: Task) -> Task:
        """Validate and add a task."""
        problems = task.validate()
        if problems:
            raise TaskValidationError(f"Task is invalid: {problems}")
        if task.pet_id not in self._pets:
            raise KeyError(f"No pet found with id {task.pet_id!r}")
        self._tasks[task.id] = task
        logger.info("Task added: %s for pet %s", task.__class__.__name__, task.pet_id)
        return task

    def get_tasks(self, pet_id: str = None) -> list[Task]:
        """Return all tasks, or only tasks for a specific pet."""
        tasks = list(self._tasks.values())
        if pet_id:
            tasks = [t for t in tasks if t.pet_id == pet_id]
        return tasks

    def mark_complete(self, task_id: str) -> None:
        """Mark a task as done."""
        if task_id not in self._tasks:
            raise KeyError(f"No task found with id {task_id!r}")
        self._tasks[task_id].status = TaskStatus.COMPLETE
        logger.info("Task completed: %s", task_id)

    # -- Schedule --

    def build_schedule(self, now: datetime) -> Schedule:
        """Build today's urgency-sorted schedule."""
        return self._scheduler.build(self.get_pets(), self.get_tasks(), now)

    # -- AI plan --

    def plan_ai_day(self, now: datetime) -> PlanResult:
        """Ask the AI agent to plan the day. Falls back to the regular schedule if agent isn't ready."""
        schedule = self.build_schedule(now)
        try:
            from ai.agent import Agent
            return Agent().plan_day(schedule, {"pets": self.get_pets()})
        except ImportError:
            logger.warning("AI agent not available yet — using regular schedule.")
            return PlanResult(
                ordered_tasks = schedule.tasks,
                time_windows  = {},
                rationales    = ["AI agent not available yet."],
                flagged_risks = [],
                is_ai_planned = False,
            )

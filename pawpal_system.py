"""Logic layer for PawPal+: domain models, scheduling, and the application facade."""

from __future__ import annotations

import dataclasses
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────


class TaskValidationError(Exception):
    """Raised when a task fails domain validation."""


class SchedulingConflict(Exception):
    """Raised when a hard scheduling rule is violated."""


# ── Status enum ───────────────────────────────────────────────────────────────


class TaskStatus(str, Enum):
    PENDING = "pending"
    COMPLETE = "complete"
    SKIPPED = "skipped"


# ── Pet ───────────────────────────────────────────────────────────────────────


@dataclass
class Pet:
    """A pet under care in PawPal+."""

    name: str
    species: str
    breed: str
    weight: float
    notes: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON storage."""
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Pet:
        """Deserialize a Pet from a plain dict.

        Args:
            data: Dict with keys matching Pet fields.

        Returns:
            A Pet instance.
        """
        return Pet(
            name=data["name"],
            species=data["species"],
            breed=data["breed"],
            weight=float(data["weight"]),
            notes=data.get("notes", ""),
            id=data.get("id", str(uuid.uuid4())),
        )


# ── Task (abstract base) ──────────────────────────────────────────────────────


@dataclass
class Task(ABC):
    """
    Abstract base for all pet-care tasks.

    All fields are inherited by subclasses through dataclass inheritance.
    Subclasses must implement urgency_score and validate.
    """

    pet_id: str
    due_at: datetime
    priority: int
    status: TaskStatus = TaskStatus.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @abstractmethod
    def urgency_score(self, now: datetime) -> float:
        """Return urgency as a float; higher means more urgent.

        Overdue tasks score above 10.0 by convention so they always
        sort ahead of upcoming tasks.

        Args:
            now: Current reference time. Must be injected; never call
                 datetime.now() inside this method.
        """

    @abstractmethod
    def validate(self) -> list[str]:
        """Return a list of violation strings; an empty list means valid.

        Returns:
            List of human-readable violation descriptions.
        """

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict including the 'type' key for reconstruction.

        Returns:
            JSON-serializable dict.
        """
        d = dataclasses.asdict(self)
        d["type"] = self.__class__.__name__.lower().replace("task", "")
        d["due_at"] = self.due_at.isoformat()
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Factory: construct the correct Task subclass from a serialized dict.

        Args:
            data: Must contain 'type', 'pet_id', 'due_at', 'priority'.

        Returns:
            The appropriate Task subclass instance.

        Raises:
            TaskValidationError: If 'type' is missing or unrecognised.
        """
        type_map: dict[str, type[Task]] = {
            "feeding": FeedingTask,
            "walk": WalkTask,
            "medication": MedicationTask,
            "appointment": AppointmentTask,
        }
        task_type = data.get("type", "")
        klass = type_map.get(task_type)
        if klass is None:
            raise TaskValidationError(f"Unknown task type: {task_type!r}")
        return klass(
            pet_id=data["pet_id"],
            due_at=datetime.fromisoformat(data["due_at"]),
            priority=int(data.get("priority", 0)),
            status=TaskStatus(data.get("status", TaskStatus.PENDING.value)),
            metadata=data.get("metadata", {}),
            id=data.get("id", str(uuid.uuid4())),
        )

    def _urgency(
        self,
        now: datetime,
        overdue_base: float,
        window_hours: float,
        peak: float,
    ) -> float:
        """Shared urgency formula for all subclasses.

        Returns overdue_base + (hours overdue) when past due; scales linearly
        from 0.0 up to peak within window_hours of the due time.
        """
        delta_s = (self.due_at - now).total_seconds()
        if delta_s <= 0:
            return overdue_base + abs(delta_s) / 3600.0
        window_s = window_hours * 3600.0
        if delta_s >= window_s:
            return 0.0
        return peak * (1.0 - delta_s / window_s)


# ── Task subclasses ───────────────────────────────────────────────────────────


@dataclass
class FeedingTask(Task):
    """Scheduled feeding. Optional metadata key: portion_size (float)."""

    def urgency_score(self, now: datetime) -> float:
        return self._urgency(now, overdue_base=10.0, window_hours=4.0, peak=5.0)

    def validate(self) -> list[str]:
        violations: list[str] = []
        portion = self.metadata.get("portion_size")
        if portion is not None and float(portion) <= 0:
            violations.append("portion_size must be positive")
        return violations


@dataclass
class WalkTask(Task):
    """Scheduled walk. Optional metadata key: duration_minutes (float)."""

    def urgency_score(self, now: datetime) -> float:
        return self._urgency(now, overdue_base=8.0, window_hours=3.0, peak=4.0)

    def validate(self) -> list[str]:
        violations: list[str] = []
        duration = self.metadata.get("duration_minutes")
        if duration is not None and float(duration) <= 0:
            violations.append("duration_minutes must be positive")
        return violations


@dataclass
class MedicationTask(Task):
    """Scheduled medication. Required metadata key: medication_name (str).
    Optional: dose (float).
    """

    def urgency_score(self, now: datetime) -> float:
        # Medication is always the highest-priority task type.
        return self._urgency(now, overdue_base=100.0, window_hours=1.0, peak=20.0)

    def validate(self) -> list[str]:
        violations: list[str] = []
        if not self.metadata.get("medication_name"):
            violations.append("medication_name is required in metadata")
        dose = self.metadata.get("dose")
        if dose is not None and float(dose) <= 0:
            violations.append("dose must be positive")
        return violations


@dataclass
class AppointmentTask(Task):
    """Vet or grooming appointment. Required metadata: location or vet_name."""

    def urgency_score(self, now: datetime) -> float:
        return self._urgency(now, overdue_base=50.0, window_hours=2.0, peak=15.0)

    def validate(self) -> list[str]:
        violations: list[str] = []
        if not self.metadata.get("location") and not self.metadata.get("vet_name"):
            violations.append("Either location or vet_name is required in metadata")
        return violations


# ── Schedule ──────────────────────────────────────────────────────────────────


@dataclass
class Schedule:
    """An ordered collection of tasks for a specific time window."""

    tasks: list[Task]
    window_start: datetime
    window_end: datetime

    def filter(self, criteria: Callable[[Task], bool]) -> Schedule:
        """Return a new Schedule containing only tasks that satisfy criteria.

        Args:
            criteria: A callable that returns True for tasks to keep.

        Returns:
            A new Schedule with the same window but filtered tasks.
        """
        return Schedule(
            tasks=[t for t in self.tasks if criteria(t)],
            window_start=self.window_start,
            window_end=self.window_end,
        )

    def sort_by_urgency(self, now: datetime) -> Schedule:
        """Return a new Schedule with tasks sorted highest-urgency first.

        Args:
            now: Reference time passed to each task's urgency_score.

        Returns:
            A new Schedule with tasks in descending urgency order.
        """
        return Schedule(
            tasks=sorted(self.tasks, key=lambda t: t.urgency_score(now), reverse=True),
            window_start=self.window_start,
            window_end=self.window_end,
        )

    def detect_conflicts(self) -> list[str]:
        """Return descriptions of same-pet, same-time scheduling conflicts.

        Fine-grained rule conflicts (e.g., walk-after-meal cooldowns) are
        handled in Scheduler.apply_rules, not here.

        Returns:
            List of conflict description strings; empty means no conflicts.
        """
        seen: dict[tuple[str, datetime], str] = {}
        conflicts: list[str] = []
        for task in self.tasks:
            key = (task.pet_id, task.due_at)
            if key in seen:
                conflicts.append(
                    f"Tasks {seen[key]!r} and {task.id!r} for pet {task.pet_id!r}"
                    f" both scheduled at {task.due_at.isoformat()}"
                )
            else:
                seen[key] = task.id
        return conflicts


# ── Scheduler ─────────────────────────────────────────────────────────────────


class Scheduler:
    """Stateless, pure scheduler. No I/O; inject now for determinism in tests."""

    def build(self, pets: list[Pet], tasks: list[Task], now: datetime) -> Schedule:
        """Build an urgency-ordered Schedule covering the day that contains now.

        Includes all PENDING tasks due before end-of-day, plus overdue tasks
        from previous days that are still pending.

        Args:
            pets: All registered pets (available for future per-pet rules).
            tasks: All known tasks across all pets.
            now: Reference time. Must be injected; never call datetime.now() here.

        Returns:
            An urgency-sorted Schedule for the current day.
        """
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        in_window = [
            t for t in tasks
            if t.status == TaskStatus.PENDING and t.due_at < day_end
        ]

        schedule = Schedule(tasks=in_window, window_start=day_start, window_end=day_end)
        schedule = self.apply_rules(schedule, now)
        return schedule.sort_by_urgency(now)

    def apply_rules(self, schedule: Schedule, now: datetime) -> Schedule:
        """Apply domain scheduling rules and return a corrected Schedule.

        Each rule should be a pure function with the signature
        (Schedule, now: datetime) -> Schedule. Wire new rules here; see
        CLAUDE.md §9 for the convention.

        Args:
            schedule: The schedule to check and correct.
            now: Reference time for time-sensitive rules.

        Returns:
            A Schedule that satisfies all registered rules.
        """
        return schedule


# ── PlanResult (DTO) ──────────────────────────────────────────────────────────


@dataclass
class PlanResult:
    """Structured output returned by Agent.plan_day or the deterministic fallback."""

    ordered_tasks: list[Task]
    time_windows: dict[str, str]
    rationales: list[str]
    flagged_risks: list[str]
    is_ai_planned: bool = False


# ── PawPalSystem (Facade) ─────────────────────────────────────────────────────


class PawPalSystem:
    """
    Application facade: the single entry point for UI, demo, and agent code.

    Holds pets and tasks in memory. Persistent storage via storage.py is not
    yet wired; connect it in __init__ once storage.py exists.
    """

    def __init__(self) -> None:
        self._pets: dict[str, Pet] = {}
        self._tasks: dict[str, Task] = {}
        self._scheduler = Scheduler()

    # ── Pets ──────────────────────────────────────────────────────────────────

    def add_pet(self, pet: Pet) -> Pet:
        """Register a pet and return it.

        Args:
            pet: A Pet instance. A uuid is assigned if pet.id is empty.

        Returns:
            The stored Pet, possibly with a generated id.
        """
        if not pet.id:
            pet = dataclasses.replace(pet, id=str(uuid.uuid4()))
        self._pets[pet.id] = pet
        logger.info("Pet added: %s (%s)", pet.name, pet.id)
        return pet

    def get_pets(self) -> list[Pet]:
        """Return all registered pets."""
        return list(self._pets.values())

    def get_pet(self, pet_id: str) -> Pet:
        """Return a single pet by id.

        Args:
            pet_id: The pet's uuid string.

        Raises:
            KeyError: If no pet with that id is registered.
        """
        if pet_id not in self._pets:
            raise KeyError(f"Pet not found: {pet_id!r}")
        return self._pets[pet_id]

    # ── Tasks ──────────────────────────────────────────────────────────────────

    def add_task(self, task: Task) -> Task:
        """Validate and register a task.

        Args:
            task: Any Task subclass instance.

        Returns:
            The stored task.

        Raises:
            TaskValidationError: If task.validate() returns any violations.
            KeyError: If task.pet_id does not match a registered pet.
        """
        violations = task.validate()
        if violations:
            raise TaskValidationError(f"Task {task.id!r} is invalid: {violations}")
        if task.pet_id not in self._pets:
            raise KeyError(f"No pet with id {task.pet_id!r}")
        self._tasks[task.id] = task
        logger.info(
            "Task added: %s %s for pet %s due %s",
            task.__class__.__name__,
            task.id,
            task.pet_id,
            task.due_at,
        )
        return task

    def get_tasks(self, pet_id: str | None = None) -> list[Task]:
        """Return all tasks, optionally filtered to a single pet.

        Args:
            pet_id: If provided, return only tasks whose pet_id matches.

        Returns:
            List of Task instances.
        """
        tasks = list(self._tasks.values())
        if pet_id is not None:
            tasks = [t for t in tasks if t.pet_id == pet_id]
        return tasks

    def mark_complete(self, task_id: str) -> None:
        """Mark a task as completed.

        Args:
            task_id: The task's uuid string.

        Raises:
            KeyError: If no task with that id exists.
        """
        if task_id not in self._tasks:
            raise KeyError(f"Task not found: {task_id!r}")
        self._tasks[task_id].status = TaskStatus.COMPLETE
        logger.info("Task completed: %s", task_id)

    # ── Schedule ──────────────────────────────────────────────────────────────

    def build_schedule(self, now: datetime) -> Schedule:
        """Return the urgency-ordered deterministic schedule for today.

        Args:
            now: Reference time. Must be injected for test determinism.

        Returns:
            A Schedule covering the calendar day that contains now.
        """
        return self._scheduler.build(
            pets=self.get_pets(),
            tasks=self.get_tasks(),
            now=now,
        )

    # ── AI plan ───────────────────────────────────────────────────────────────

    def plan_ai_day(self, now: datetime) -> PlanResult:
        """Return an AI-enhanced care plan for the day.

        Delegates to ai.agent.Agent when available. Falls back to the
        deterministic schedule (is_ai_planned=False) if the agent module
        has not been created yet.

        Args:
            now: Reference time for the underlying schedule.

        Returns:
            A PlanResult — AI-generated or deterministic fallback.
        """
        schedule = self.build_schedule(now)
        try:
            from ai.agent import Agent  # noqa: PLC0415
            return Agent().plan_day(schedule, {"pets": self.get_pets()})
        except ImportError:
            logger.warning("ai.agent not available; returning deterministic schedule.")
            return PlanResult(
                ordered_tasks=schedule.tasks,
                time_windows={},
                rationales=["Deterministic fallback — AI agent not yet available."],
                flagged_risks=[],
                is_ai_planned=False,
            )

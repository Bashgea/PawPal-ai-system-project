"""ai/agent.py — plan → validate → repair loop. The only module that calls the model."""

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import config
from ai.ollama_client import MalformedResponseError, ModelUnavailableError, OllamaClient
from ai.prompts import PLAN_PROMPT_V1, REPAIR_PROMPT_V1
from pawpal_system import AppointmentTask, FeedingTask, MedicationTask, PlanResult, Schedule, WalkTask

logger = logging.getLogger(__name__)


class PlanInvalidError(Exception):
    """The AI plan did not pass validation after all repair attempts."""


# ── Public class ──────────────────────────────────────────────────────────────

class Agent:
    """Orchestrates plan → validate → repair using a local Ollama model.

    Args:
        client: OllamaClient to use. Pass a mock in tests; omit for production.
    """

    def __init__(self, client: OllamaClient | None = None) -> None:
        self._client = client or OllamaClient()

    def plan_day(self, schedule: Schedule, context: dict[str, Any]) -> PlanResult:
        """Return an AI-planned PlanResult, falling back to the deterministic schedule on any failure.

        Args:
            schedule: Today's deterministic Schedule from Scheduler.build().
            context:  Extra data — currently expects {"pets": list[Pet]}.

        Returns:
            PlanResult with is_ai_planned=True on success, False on fallback.
        """
        if not config.ENABLE_AI:
            logger.info("ENABLE_AI=false — using deterministic schedule.")
            return _deterministic_result(schedule, "AI planning is disabled (ENABLE_AI=false)")

        snippets = _retrieve_snippets(schedule, context)
        prompt   = _build_plan_prompt(schedule, context, snippets)

        logger.info("Requesting AI plan from %s/%s.", config.OLLAMA_HOST, config.PAWPAL_MODEL)
        try:
            plan = self._client.complete_json(prompt)
        except ModelUnavailableError as exc:
            logger.warning("Ollama unavailable (%s) — falling back.", exc)
            return _deterministic_result(schedule, f"Ollama unavailable: {exc}")
        except MalformedResponseError as exc:
            logger.warning("Malformed response (%s) — falling back.", exc)
            return _deterministic_result(schedule, f"Model returned malformed response: {exc}")

        # Repair loop — outer try catches any unexpected runtime error and falls back safely.
        try:
            for i in range(config.MAX_REPAIR_ITERS + 1):
                violations = _validate(plan, schedule)
                if not violations:
                    break
                if i == config.MAX_REPAIR_ITERS:
                    logger.warning(
                        "Plan still invalid after %d repair(s) — falling back.",
                        config.MAX_REPAIR_ITERS,
                    )
                    return _deterministic_result(
                        schedule,
                        f"AI plan invalid after {config.MAX_REPAIR_ITERS} repair attempt(s)",
                    )
                logger.info("Repair attempt %d — %d violation(s).", i + 1, len(violations))
                repair_prompt = _build_repair_prompt(plan, violations, schedule)
                try:
                    plan = self._client.complete_json(repair_prompt)
                except ModelUnavailableError as exc:
                    logger.warning("Repair failed — Ollama unavailable (%s) — falling back.", exc)
                    return _deterministic_result(
                        schedule, f"Repair failed — Ollama unavailable: {exc}"
                    )
                except MalformedResponseError as exc:
                    logger.warning("Repair failed — malformed response (%s) — falling back.", exc)
                    return _deterministic_result(
                        schedule, f"Repair failed — malformed response: {exc}"
                    )
            return _build_result(plan, schedule)
        except Exception as exc:
            logger.error("Unexpected error during AI planning: %s", exc, exc_info=True)
            return _deterministic_result(schedule, f"Unexpected error during AI planning: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _deterministic_result(schedule: Schedule, reason: str = "AI agent not available") -> PlanResult:
    return PlanResult(
        ordered_tasks = schedule.tasks,
        time_windows  = {},
        rationales    = [f"{reason} — using deterministic schedule."],
        flagged_risks = [],
        is_ai_planned = False,
    )


def _retrieve_snippets(schedule: Schedule, context: dict[str, Any]) -> str:
    if not config.ENABLE_RAG:
        return "(RAG disabled)"
    try:
        from ai.rag import RAG
        rag   = RAG(config.KNOWLEDGE_DIR)
        query = "pet care schedule medication feeding walk"
        return "\n".join(rag.retrieve(query))
    except Exception as exc:  # rag is optional; never crash the agent
        logger.debug("RAG unavailable: %s", exc)
        return "(no snippets)"


def _build_plan_prompt(schedule: Schedule, context: dict[str, Any], snippets: str) -> str:
    tasks_json = json.dumps([t.to_dict() for t in schedule.tasks], indent=2)
    pets_json  = json.dumps(
        [p.to_dict() for p in context.get("pets", [])], indent=2
    )
    return PLAN_PROMPT_V1.format(
        tasks_json   = tasks_json,
        pets_json    = pets_json,
        snippets     = snippets,
        window_start = schedule.window_start.isoformat(),
        window_end   = schedule.window_end.isoformat(),
    )


def _build_repair_prompt(plan: dict, violations: list[str], schedule: Schedule) -> str:
    return REPAIR_PROMPT_V1.format(
        plan_json    = json.dumps(plan, indent=2),
        violations   = "\n".join(f"- {v}" for v in violations),
        window_start = schedule.window_start.isoformat(),
        window_end   = schedule.window_end.isoformat(),
    )


def _validate(plan: dict, schedule: Schedule) -> list[str]:
    """Check hard invariants. Returns violations; never raises.

    Returns:
        List of violation strings. Empty list means the plan is valid.
    """
    violations: list[str] = []
    try:
        # 1. ordered_task_ids must be a list
        ids = plan.get("ordered_task_ids")
        if not isinstance(ids, list):
            violations.append("ordered_task_ids must be a list.")
            return violations  # can't check further without the list

        schedule_ids = {t.id for t in schedule.tasks}

        # 2. No invented IDs
        for oid in ids:
            if oid not in schedule_ids:
                violations.append(f"Unknown task id in plan: {oid!r}")

        # 3. No missing tasks
        for tid in schedule_ids:
            if tid not in ids:
                violations.append(f"Task {tid!r} is missing from ordered_task_ids.")

        # 4 & 5. suggested_times must be a dict with parseable ISO datetimes within the window
        times: dict[str, datetime] = {}
        suggested = plan.get("suggested_times", {})
        if not isinstance(suggested, dict):
            violations.append(
                f"suggested_times must be a dict, got {type(suggested).__name__!r}."
            )
            suggested = {}  # continue; remaining checks still run with empty dict

        for tid, iso in suggested.items():
            try:
                dt = datetime.fromisoformat(iso)
            except (ValueError, TypeError):
                violations.append(
                    f"suggested_times[{tid!r}] is not a valid ISO-8601 datetime: {iso!r}"
                )
                continue
            times[tid] = dt
            if not (schedule.window_start <= dt < schedule.window_end):
                violations.append(
                    f"suggested_times[{tid!r}] = {iso!r} is outside the day window "
                    f"({schedule.window_start.isoformat()} – {schedule.window_end.isoformat()})."
                )

        # 6. Appointment times must not change
        for task in schedule.tasks:
            if isinstance(task, AppointmentTask) and task.id in times:
                if times[task.id] != task.due_at:
                    violations.append(
                        f"Appointment {task.id!r} was moved from {task.due_at.isoformat()} "
                        f"to {times[task.id].isoformat()} — appointments cannot be moved."
                    )

        # 7. Medication spacing >= 8 hours
        med_times: dict[tuple[str, str], list[datetime]] = {}
        for task in schedule.tasks:
            if isinstance(task, MedicationTask) and task.id in times:
                key = (task.pet_id, task.metadata.get("medication_name", ""))
                med_times.setdefault(key, []).append(times[task.id])

        for (pet_id, med_name), dts in med_times.items():
            for a, b in zip(sorted(dts), sorted(dts)[1:]):
                if b - a < timedelta(hours=8):
                    violations.append(
                        f"Medication {med_name!r} for pet {pet_id!r}: doses at "
                        f"{a.isoformat()} and {b.isoformat()} are less than 8 hours apart."
                    )

        # 8. No walk within 30 min after a feeding (per pet)
        feedings_by_pet: dict[str, list[datetime]] = {}
        for task in schedule.tasks:
            if isinstance(task, FeedingTask) and task.id in times:
                feedings_by_pet.setdefault(task.pet_id, []).append(times[task.id])

        for task in schedule.tasks:
            if isinstance(task, WalkTask) and task.id in times:
                walk_time = times[task.id]
                for feed_time in feedings_by_pet.get(task.pet_id, []):
                    if feed_time <= walk_time < feed_time + timedelta(minutes=30):
                        violations.append(
                            f"Walk {task.id!r} for pet {task.pet_id!r} at {walk_time.isoformat()} "
                            f"is within 30 minutes of feeding at {feed_time.isoformat()}."
                        )

    except Exception as exc:
        logger.error("_validate raised unexpectedly: %s", exc, exc_info=True)
        violations.append(f"Validation error: {exc}")

    return violations


def _build_result(plan: dict, schedule: Schedule) -> PlanResult:
    """Convert a validated plan dict into a PlanResult."""
    task_map      = {t.id: t for t in schedule.tasks}
    ordered_tasks = [task_map[tid] for tid in plan["ordered_task_ids"] if tid in task_map]
    time_windows  = plan.get("suggested_times", {})
    rationales    = plan.get("rationales", [])
    flagged_risks = plan.get("flagged_risks", [])
    return PlanResult(
        ordered_tasks = ordered_tasks,
        time_windows  = time_windows,
        rationales    = rationales,
        flagged_risks = flagged_risks,
        is_ai_planned = True,
    )

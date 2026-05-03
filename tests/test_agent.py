"""Unit tests for Agent.plan_day — uses a mocked OllamaClient."""

import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from ai.agent import Agent, _validate
from ai.ollama_client import MalformedResponseError, ModelUnavailableError
from pawpal_system import (
    AppointmentTask, FeedingTask, MedicationTask, Schedule, WalkTask,
)

# ── Fixed test constants ──────────────────────────────────────────────────────

DAY_START = datetime(2026, 5, 3,  0,  0)
DAY_END   = datetime(2026, 5, 4,  0,  0)
PET       = "pet-001"


def at(hour, minute=0):
    return datetime(2026, 5, 3, hour, minute)

def sched(*tasks):
    return Schedule(list(tasks), DAY_START, DAY_END)

def feeding(t):
    return FeedingTask(pet_id=PET, due_at=t, priority=1)

def walk(t):
    return WalkTask(pet_id=PET, due_at=t, priority=1)

def med(t, name="Heartguard"):
    return MedicationTask(pet_id=PET, due_at=t, priority=1, metadata={"medication_name": name})

def appt(t):
    return AppointmentTask(pet_id=PET, due_at=t, priority=1, metadata={"vet_name": "Dr. Smith"})

def mock_client(response_dict: dict):
    """Return an OllamaClient mock that always returns response_dict from complete_json."""
    client = MagicMock()
    client.complete_json.return_value = response_dict
    return client

def good_plan(*tasks) -> dict:
    """Build a valid plan dict for the given tasks (uses their original due_at)."""
    return {
        "ordered_task_ids": [t.id for t in tasks],
        "suggested_times":  {t.id: t.due_at.isoformat() for t in tasks},
        "rationales":       [f"Task {t.id}" for t in tasks],
        "flagged_risks":    [],
    }


# ── Happy path ────────────────────────────────────────────────────────────────

def test_plan_day_returns_ai_result_on_success():
    """A valid model response produces a PlanResult with is_ai_planned=True."""
    f = feeding(at(8))
    w = walk(at(10))
    schedule = sched(f, w)

    agent = Agent(client=mock_client(good_plan(f, w)))
    result = agent.plan_day(schedule, {"pets": []})

    assert result.is_ai_planned is True
    assert [t.id for t in result.ordered_tasks] == [f.id, w.id]


def test_plan_day_preserves_rationales_and_risks():
    """Rationales and flagged_risks from the model are passed through unchanged."""
    f = feeding(at(8))
    plan = good_plan(f)
    plan["rationales"]    = ["Feed early for energy."]
    plan["flagged_risks"] = ["Dog has sensitive stomach."]

    agent = Agent(client=mock_client(plan))
    result = agent.plan_day(sched(f), {"pets": []})

    assert result.rationales    == ["Feed early for energy."]
    assert result.flagged_risks == ["Dog has sensitive stomach."]


# ── Fallback behaviour ────────────────────────────────────────────────────────

def test_fallback_on_model_unavailable():
    """ModelUnavailableError from the client causes a deterministic fallback."""
    f = feeding(at(8))
    client = MagicMock()
    client.complete_json.side_effect = ModelUnavailableError("Ollama is down")

    result = Agent(client=client).plan_day(sched(f), {"pets": []})

    assert result.is_ai_planned is False


def test_fallback_on_malformed_response():
    """MalformedResponseError causes a deterministic fallback."""
    f = feeding(at(8))
    client = MagicMock()
    client.complete_json.side_effect = MalformedResponseError("not JSON")

    result = Agent(client=client).plan_day(sched(f), {"pets": []})

    assert result.is_ai_planned is False


# ── Repair loop ───────────────────────────────────────────────────────────────

def test_plan_repaired_on_first_violation():
    """If the first plan is invalid, the agent repairs it and returns is_ai_planned=True."""
    f = feeding(at(8))
    w = walk(at(10))

    bad_plan  = good_plan(f, w)
    bad_plan["ordered_task_ids"] = [f.id]   # missing w — invalid

    fixed_plan = good_plan(f, w)

    client = MagicMock()
    client.complete_json.side_effect = [bad_plan, fixed_plan]

    result = Agent(client=client).plan_day(sched(f, w), {"pets": []})

    assert result.is_ai_planned is True
    assert client.complete_json.call_count == 2


def test_fallback_after_max_repairs_exhausted():
    """If every repair attempt returns an invalid plan, fall back to deterministic."""
    f = feeding(at(8))
    w = walk(at(10))

    always_bad = good_plan(f, w)
    always_bad["ordered_task_ids"] = [f.id]   # always missing w

    client = MagicMock()
    client.complete_json.return_value = always_bad

    result = Agent(client=client).plan_day(sched(f, w), {"pets": []})

    assert result.is_ai_planned is False


# ── _validate: individual invariants ─────────────────────────────────────────

def test_validate_detects_missing_task():
    f = feeding(at(8))
    w = walk(at(10))
    plan = {
        "ordered_task_ids": [f.id],   # w missing
        "suggested_times":  {f.id: at(8).isoformat(), w.id: at(10).isoformat()},
    }
    violations = _validate(plan, sched(f, w))
    assert any(w.id in v for v in violations)


def test_validate_detects_invented_id():
    f = feeding(at(8))
    plan = {
        "ordered_task_ids": [f.id, "fake-id-xyz"],
        "suggested_times":  {f.id: at(8).isoformat()},
    }
    violations = _validate(plan, sched(f))
    assert any("fake-id-xyz" in v for v in violations)


def test_validate_detects_moved_appointment():
    a = appt(at(10))
    plan = {
        "ordered_task_ids": [a.id],
        "suggested_times":  {a.id: at(11).isoformat()},   # moved by 1 hour
    }
    violations = _validate(plan, sched(a))
    assert any("appointment" in v.lower() or "moved" in v.lower() for v in violations)


def test_validate_detects_med_spacing_violation():
    d1 = med(at(8))
    d2 = med(at(10))   # only 2 h apart
    plan = {
        "ordered_task_ids": [d1.id, d2.id],
        "suggested_times":  {d1.id: at(8).isoformat(), d2.id: at(10).isoformat()},
    }
    violations = _validate(plan, sched(d1, d2))
    assert any("8 hours" in v for v in violations)


def test_validate_detects_walk_cooldown_violation():
    f = feeding(at(8))
    w = walk(at(8, 15))   # 15 min after feeding — inside 30-min cooldown
    plan = {
        "ordered_task_ids": [f.id, w.id],
        "suggested_times":  {f.id: at(8).isoformat(), w.id: at(8, 15).isoformat()},
    }
    violations = _validate(plan, sched(f, w))
    assert any("30 minutes" in v for v in violations)


def test_validate_passes_for_valid_plan():
    f  = feeding(at(8))
    w  = walk(at(10))
    d1 = med(at(9),  "H")
    d2 = med(at(18), "H")
    a  = appt(at(14))
    tasks = [f, w, d1, d2, a]
    plan = good_plan(*tasks)
    assert _validate(plan, sched(*tasks)) == []


# ── Input type hardening ──────────────────────────────────────────────────────

def test_validate_suggested_times_not_dict_returns_violation():
    """suggested_times being a list instead of dict must produce a violation, not crash."""
    f = feeding(at(8))
    plan = {
        "ordered_task_ids": [f.id],
        "suggested_times":  [f.id, at(8).isoformat()],   # wrong type: list
    }
    violations = _validate(plan, sched(f))
    assert isinstance(violations, list)
    assert any("dict" in v for v in violations)


def test_validate_never_raises_on_garbage_plan():
    """_validate must return a violation list (never raise) for any malformed payload."""
    f = feeding(at(8))
    garbage_plans = [
        {},
        {"ordered_task_ids": "not-a-list"},
        {"ordered_task_ids": None, "suggested_times": None},
        {"ordered_task_ids": [f.id], "suggested_times": 42},
        None,  # plan itself is None — guard against callers passing bad data
    ]
    for bad in garbage_plans:
        try:
            result = _validate(bad or {}, sched(f))
            assert isinstance(result, list), f"Expected list, got {type(result)} for plan={bad!r}"
        except Exception as exc:
            pytest.fail(f"_validate raised {exc!r} for plan={bad!r}")


# ── Fallback rationale clarity ────────────────────────────────────────────────

def test_fallback_rationale_ai_disabled():
    """When ENABLE_AI is false the rationale says 'disabled', not a generic message."""
    f = feeding(at(8))
    with patch("config.ENABLE_AI", False):
        result = Agent(client=MagicMock()).plan_day(sched(f), {"pets": []})
    assert result.is_ai_planned is False
    assert any("disabled" in r.lower() for r in result.rationales)


def test_fallback_rationale_model_unavailable():
    """ModelUnavailableError produces a rationale mentioning 'unavailable'."""
    f = feeding(at(8))
    client = MagicMock()
    client.complete_json.side_effect = ModelUnavailableError("connection refused")

    result = Agent(client=client).plan_day(sched(f), {"pets": []})

    assert result.is_ai_planned is False
    assert any("unavailable" in r.lower() for r in result.rationales)


def test_fallback_rationale_malformed_response():
    """MalformedResponseError produces a rationale mentioning 'malformed'."""
    f = feeding(at(8))
    client = MagicMock()
    client.complete_json.side_effect = MalformedResponseError("not JSON")

    result = Agent(client=client).plan_day(sched(f), {"pets": []})

    assert result.is_ai_planned is False
    assert any("malformed" in r.lower() for r in result.rationales)


# ── Unexpected exception safety ───────────────────────────────────────────────

def test_fallback_on_unexpected_validation_error():
    """An unexpected exception inside the repair/validate loop still falls back gracefully."""
    f = feeding(at(8))
    plan = good_plan(f)

    client = MagicMock()
    client.complete_json.return_value = plan

    with patch("ai.agent._validate", side_effect=RuntimeError("internal boom")):
        result = Agent(client=client).plan_day(sched(f), {"pets": []})

    assert result.is_ai_planned is False
    assert any("unexpected" in r.lower() for r in result.rationales)

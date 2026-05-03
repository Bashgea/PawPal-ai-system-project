"""Unit tests for Scheduler.apply_rules — deterministic scheduling rules."""

import pytest
from datetime import datetime
from pawpal_system import (
    FeedingTask, WalkTask, MedicationTask, AppointmentTask,
    Schedule, Scheduler, SchedulingConflict,
)

# ── Fixed test constants ──────────────────────────────────────────────────────

DAY_START = datetime(2026, 5, 3,  0,  0)
DAY_END   = datetime(2026, 5, 4,  0,  0)
PET       = "pet-001"
NOW       = datetime(2026, 5, 3,  7,  0)   # 7 AM reference, injected into scheduler

scheduler = Scheduler()


# ── Helpers ───────────────────────────────────────────────────────────────────

def at(hour, minute=0):
    """Shorthand: datetime on the test day at given hour:minute."""
    return datetime(2026, 5, 3, hour, minute)

def sched(*tasks):
    return Schedule(list(tasks), DAY_START, DAY_END)

def med(t, name="Heartguard"):
    return MedicationTask(pet_id=PET, due_at=t, priority=1, metadata={"medication_name": name})

def walk(t):
    return WalkTask(pet_id=PET, due_at=t, priority=1)

def feeding(t):
    return FeedingTask(pet_id=PET, due_at=t, priority=1)

def appt(t):
    return AppointmentTask(pet_id=PET, due_at=t, priority=1, metadata={"vet_name": "Dr. Smith"})

def times(result):
    """Return {task_id: due_at} from a Schedule — easier to assert against."""
    return {t.id: t.due_at for t in result.tasks}


# ── R1: Medication spacing ────────────────────────────────────────────────────

def test_med_spacing_enforced():
    """Second dose 2 h after first should be shifted to first + 8 h."""
    d1 = med(at(8))
    d2 = med(at(10))          # gap = 2 h < 8 h

    result = scheduler.apply_rules(sched(d1, d2), NOW)
    t = times(result)

    assert t[d1.id] == at(8)   # first dose untouched
    assert t[d2.id] == at(16)  # shifted to 08:00 + 8 h


def test_med_spacing_sufficient_unchanged():
    """Doses 10 h apart should not be changed."""
    d1 = med(at(6))
    d2 = med(at(16))           # gap = 10 h > 8 h

    result = scheduler.apply_rules(sched(d1, d2), NOW)
    t = times(result)

    assert t[d1.id] == at(6)
    assert t[d2.id] == at(16)


def test_med_spacing_different_meds_independent():
    """Different medications for same pet are not constrained against each other."""
    d1 = med(at(8),  name="Heartguard")
    d2 = med(at(10), name="Bravecto")    # different med — rule does not apply

    result = scheduler.apply_rules(sched(d1, d2), NOW)
    t = times(result)

    assert t[d1.id] == at(8)
    assert t[d2.id] == at(10)            # unchanged


def test_med_spacing_unresolvable_raises():
    """Dose too late in day — next valid slot falls outside window."""
    d1 = med(at(20))
    d2 = med(at(22))    # next valid = 20:00 + 8 h = 04:00 next day → outside window

    with pytest.raises(SchedulingConflict):
        scheduler.apply_rules(sched(d1, d2), NOW)


# ── R2: Walk cooldown after feeding ───────────────────────────────────────────

def test_walk_shifted_past_feeding_cooldown():
    """Walk 15 min after feeding must be shifted to feeding + 30 min."""
    f = feeding(at(8))
    w = walk(at(8, 15))       # 15 min after — inside 30-min cooldown

    result = scheduler.apply_rules(sched(f, w), NOW)
    t = times(result)

    assert t[f.id] == at(8)       # feeding untouched
    assert t[w.id] == at(8, 30)   # walk shifted to 08:30


def test_walk_outside_cooldown_unchanged():
    """Walk 60 min after feeding is already valid — no shift."""
    f = feeding(at(8))
    w = walk(at(9))               # 60 min after — fine

    result = scheduler.apply_rules(sched(f, w), NOW)
    assert times(result)[w.id] == at(9)


def test_walk_before_feeding_unchanged():
    """Walk scheduled before feeding is not affected by the cooldown rule."""
    f = feeding(at(9))
    w = walk(at(8, 30))           # before the feeding

    result = scheduler.apply_rules(sched(f, w), NOW)
    assert times(result)[w.id] == at(8, 30)


def test_walk_cooldown_unresolvable_raises():
    """Walk that needs shifting but next valid slot is outside day window."""
    f = feeding(at(23, 45))
    w = walk(at(23, 50))    # cooldown_end = 00:15 next day → outside window

    with pytest.raises(SchedulingConflict):
        scheduler.apply_rules(sched(f, w), NOW)


# ── R3: Appointment immovability ──────────────────────────────────────────────

def test_appointment_time_never_changed():
    """AppointmentTask due_at must be identical before and after apply_rules."""
    a = appt(at(10))
    f = feeding(at(9, 45))

    result = scheduler.apply_rules(sched(a, f), NOW)
    appt_after = next(task for task in result.tasks if task.id == a.id)

    assert appt_after.due_at == at(10)


def test_appointment_collision_raises():
    """Walk shifted by R2 onto an appointment slot should raise SchedulingConflict."""
    f = feeding(at(10))
    w = walk(at(10, 15))    # R2 shifts this to 10:30
    a = appt(at(10, 30))    # appointment already at 10:30 → R3 raises

    with pytest.raises(SchedulingConflict):
        scheduler.apply_rules(sched(f, w, a), NOW)


# ── Stability: no unnecessary mutations ───────────────────────────────────────

def test_no_conflict_schedule_fully_stable():
    """A schedule that already satisfies all rules must come back unchanged."""
    f  = feeding(at(8))
    w  = walk(at(10))          # 2 h after feeding — fine
    d1 = med(at(9),  "H")
    d2 = med(at(18), "H")      # 9 h apart — fine
    a  = appt(at(14))

    tasks = [f, w, d1, d2, a]
    original = {task.id: task.due_at for task in tasks}

    result = scheduler.apply_rules(sched(*tasks), NOW)

    assert {task.id: task.due_at for task in result.tasks} == original

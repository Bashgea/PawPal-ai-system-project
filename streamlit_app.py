"""streamlit_app.py — PawPal+ interactive UI.

Primary entry point for the application:
    streamlit run streamlit_app.py

All domain access goes through PawPalSystem; this module only calls the
public façade and renders results with Streamlit display APIs.
"""

import logging
import os
from datetime import datetime

import streamlit as st

import config
import logging_setup
from pawpal_system import (
    AppointmentTask,
    FeedingTask,
    MedicationTask,
    PawPalSystem,
    Pet,
    SchedulingConflict,
    Task,
    TaskStatus,
    TaskValidationError,
    WalkTask,
)

# ── Logging ───────────────────────────────────────────────────────────────────
# configure() is idempotent; safe to call on every Streamlit re-run.
logging_setup.configure()

# ── Page config (must be the first Streamlit call) ────────────────────────────
st.set_page_config(
    page_title="PawPal+",
    page_icon="🐾",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Constants ─────────────────────────────────────────────────────────────────
_TASK_TYPE_LABELS: dict[str, str] = {
    "feeding":     "🍽 Feeding",
    "walk":        "🦮 Walk",
    "medication":  "💊 Medication",
    "appointment": "🏥 Appointment",
}

_STATUS_BADGES: dict[TaskStatus, str] = {
    TaskStatus.PENDING:  "🟡",
    TaskStatus.COMPLETE: "✅",
    TaskStatus.SKIPPED:  "⏭",
}


# ── Session-state helpers ─────────────────────────────────────────────────────

def _system() -> PawPalSystem:
    """Return the session-scoped PawPalSystem singleton, creating it if needed."""
    if "system" not in st.session_state:
        st.session_state["system"] = PawPalSystem(db_path=config.PAWPAL_DB)
    return st.session_state["system"]  # type: ignore[return-value]


def _invalidate_system() -> None:
    """Drop the in-memory system so it is rebuilt from disk on next access."""
    st.session_state.pop("system", None)


# ── Demo seed helper ──────────────────────────────────────────────────────────

def _seed_demo_data(sys: PawPalSystem, base: datetime) -> None:
    """Populate two pets with a realistic mix of tasks anchored to *base*.

    Args:
        sys:  The PawPalSystem to mutate.
        base: Reference datetime; tasks are scheduled relative to the same day.
    """
    today = base.replace(hour=0, minute=0, second=0, microsecond=0)

    rex = sys.add_pet(Pet(
        name="Rex", species="dog", breed="Labrador Retriever", weight=28.0,
        notes="Loves fetch. Chicken allergy — check food labels.",
    ))
    luna = sys.add_pet(Pet(
        name="Luna", species="cat", breed="Siamese", weight=4.5,
        notes="Indoor only. Shy around strangers.",
    ))

    sys.add_task(FeedingTask(
        pet_id=rex.id, due_at=today.replace(hour=7, minute=30),
        priority=2, metadata={"portion_size": 1.5},
    ))
    sys.add_task(WalkTask(
        pet_id=rex.id, due_at=today.replace(hour=8, minute=30),
        priority=1, metadata={"duration_minutes": 30},
    ))
    sys.add_task(MedicationTask(
        pet_id=rex.id, due_at=today.replace(hour=9, minute=0),
        priority=3, metadata={"medication_name": "Heartguard", "dose": 1},
    ))
    sys.add_task(FeedingTask(
        pet_id=luna.id, due_at=today.replace(hour=8, minute=0),
        priority=2, metadata={"portion_size": 0.5},
    ))
    sys.add_task(AppointmentTask(
        pet_id=luna.id, due_at=today.replace(hour=14, minute=0),
        priority=3, metadata={"vet_name": "Dr. Patel", "location": "City Vet Clinic"},
    ))


# ── Reset helper ──────────────────────────────────────────────────────────────

def _reset_db() -> bool:
    """Delete the JSON DB file and clear the in-memory session.

    Returns:
        True on success, False if the file could not be deleted.
    """
    try:
        if os.path.exists(config.PAWPAL_DB):
            os.remove(config.PAWPAL_DB)
    except OSError as exc:
        st.error(f"Could not delete DB file: {exc}")
        return False
    _invalidate_system()
    return True


# ── Rendering helpers ─────────────────────────────────────────────────────────

def _pet_name(sys: PawPalSystem, pet_id: str) -> str:
    """Resolve pet_id to a display name, with a safe fallback."""
    try:
        return sys.get_pet(pet_id).name
    except KeyError:
        return f"(unknown {pet_id[:6]}…)"


def _task_type_key(task: Task) -> str:
    return task.__class__.__name__.lower().replace("task", "")


def _render_task_card(
    task: Task,
    sys: PawPalSystem,
    *,
    show_complete_btn: bool = True,
    rationale: str = "",
    suggested_time: str = "",
    rank: int | None = None,
) -> None:
    """Render one task as a collapsible card.

    Args:
        task:              The task to render.
        sys:               PawPalSystem for pet-name lookup.
        show_complete_btn: Whether to show the mark-complete button.
        rationale:         AI rationale text (displayed in italics when provided).
        suggested_time:    AI-suggested ISO time string (displayed when provided).
        rank:              Optional 1-based rank prefix from the AI plan.
    """
    badge = _STATUS_BADGES.get(task.status, "")
    t_key = _task_type_key(task)
    label = _TASK_TYPE_LABELS.get(t_key, t_key)
    pet   = _pet_name(sys, task.pet_id)
    time_str = f"→ `{suggested_time}`" if suggested_time else f"@ {task.due_at.strftime('%H:%M')}"
    rank_str = f"**{rank}.** " if rank is not None else ""

    with st.expander(f"{badge} {rank_str}{label} — {pet}  {time_str}", expanded=False):
        col_meta, col_action = st.columns([3, 1])
        with col_meta:
            st.markdown(f"**Due:** {task.due_at.strftime('%Y-%m-%d %H:%M')}  •  **Priority:** {task.priority}")
            if task.metadata:
                st.markdown("  ".join(f"**{k}:** {v}" for k, v in task.metadata.items()))
            if rationale:
                st.markdown(f"_{rationale}_")
            st.caption(f"ID: `{task.id[:8]}…`")
        with col_action:
            if show_complete_btn and task.status == TaskStatus.PENDING:
                if st.button("✅ Complete", key=f"complete_{task.id}"):
                    try:
                        sys.mark_complete(task.id)
                        st.rerun()
                    except KeyError as exc:
                        st.error(f"Task not found: {exc}")


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🐾 PawPal+")
    st.caption("Local-first pet care assistant")
    st.divider()

    # ── Config (read-only) ────────────────────────────────────────────────────
    st.subheader("⚙️ Config")
    abs_db = os.path.abspath(config.PAWPAL_DB)
    st.text(f"DB: …{abs_db[-40:]}" if len(abs_db) > 42 else f"DB: {abs_db}")

    ai_col, rag_col = st.columns(2)
    ai_col.metric("AI",  "ON ✅" if config.ENABLE_AI  else "OFF ❌")
    rag_col.metric("RAG", "ON ✅" if config.ENABLE_RAG else "OFF ❌")
    st.caption(f"Model: `{config.PAWPAL_MODEL}`")
    st.divider()

    # ── Demo clock (injected `now`) ───────────────────────────────────────────
    st.subheader("⏱️ Demo clock")
    _now_default = datetime.now()
    _now_date = st.date_input(
        "Date", value=_now_default.date(), key="sb_now_date",
    )
    _now_time = st.time_input(
        "Time",
        value=_now_default.time().replace(second=0, microsecond=0),
        key="sb_now_time",
        step=300,
    )
    injected_now = datetime.combine(_now_date, _now_time)
    st.caption(f"Clock: `{injected_now.strftime('%a %Y-%m-%d  %H:%M')}`")
    st.divider()

    # ── Developer tools ───────────────────────────────────────────────────────
    st.subheader("🛠️ Developer tools")

    _sb_sys  = _system()
    _sb_pets = _sb_sys.get_pets()
    _seed_disabled = bool(_sb_pets)
    _seed_tip = (
        f"{len(_sb_pets)} pet(s) already in DB — reset first to re-seed."
        if _seed_disabled else
        "Creates Rex (dog) + Luna (cat) with 5 mixed tasks."
    )

    if st.button(
        "🌱 Seed demo data",
        use_container_width=True,
        disabled=_seed_disabled,
        help=_seed_tip,
    ):
        try:
            _seed_demo_data(_sb_sys, injected_now)
            st.toast("Demo data seeded!", icon="🌱")
            st.rerun()
        except (TaskValidationError, KeyError, SchedulingConflict) as exc:
            st.error(f"Seed failed: {exc}")

    st.divider()
    st.markdown("**⚠️ Danger zone**")
    _confirm_reset = st.checkbox("Confirm — wipe all data", key="confirm_reset")
    if _confirm_reset:
        if st.button("🗑️ Reset DB", type="primary", use_container_width=True):
            if _reset_db():
                st.toast("Database cleared. Starting fresh.", icon="🗑️")
                st.rerun()


# ── Main header ───────────────────────────────────────────────────────────────

st.title("🐾 PawPal+")
st.caption(
    f"Pet care assistant  •  DB: `{config.PAWPAL_DB}`  "
    f"•  AI: {'enabled' if config.ENABLE_AI else 'disabled'}"
)

tab_pets, tab_tasks, tab_schedule, tab_ai = st.tabs([
    "🐾 Pets",
    "📋 Tasks",
    "📅 Today's Schedule",
    "🤖 AI Plan",
])


# ── Tab 1: Pets ───────────────────────────────────────────────────────────────

with tab_pets:
    _sys  = _system()
    _pets = _sys.get_pets()

    col_list, col_form = st.columns([1, 1], gap="large")

    with col_list:
        st.subheader(f"Registered pets ({len(_pets)})")
        if not _pets:
            st.info("No pets yet. Add one using the form, or use **Seed demo data** in the sidebar.")
        else:
            for _pet in _pets:
                with st.expander(
                    f"**{_pet.name}** — {_pet.species.title()} ({_pet.breed})",
                    expanded=False,
                ):
                    _c1, _c2 = st.columns(2)
                    _c1.markdown(f"**Species:** {_pet.species.title()}")
                    _c1.markdown(f"**Breed:** {_pet.breed or '—'}")
                    _c1.markdown(f"**Weight:** {_pet.weight} kg")
                    _c2.markdown(f"**ID:** `{_pet.id[:8]}…`")
                    if _pet.notes:
                        _c2.markdown(f"**Notes:** {_pet.notes}")

    with col_form:
        st.subheader("Add a pet")
        with st.form("add_pet_form", clear_on_submit=True):
            _p_name    = st.text_input("Name *")
            _p_species = st.selectbox("Species", ["dog", "cat", "rabbit", "bird", "other"])
            _p_breed   = st.text_input("Breed")
            _p_weight  = st.number_input("Weight (kg)", min_value=0.1, value=5.0, step=0.1, format="%.1f")
            _p_notes   = st.text_area("Notes", height=80, placeholder="Allergies, personality, anything useful…")

            if st.form_submit_button("Add pet", type="primary", use_container_width=True):
                if not _p_name.strip():
                    st.error("Name is required.")
                else:
                    try:
                        _sys.add_pet(Pet(
                            name    = _p_name.strip(),
                            species = _p_species,
                            breed   = _p_breed.strip(),
                            weight  = _p_weight,
                            notes   = _p_notes.strip(),
                        ))
                        st.rerun()
                    except (KeyError, ValueError) as exc:
                        st.error(f"Could not add pet: {exc}")


# ── Tab 2: Tasks ──────────────────────────────────────────────────────────────

with tab_tasks:
    _sys   = _system()
    _pets  = _sys.get_pets()
    _tasks = _sys.get_tasks()

    col_list, col_form = st.columns([1, 1], gap="large")

    with col_list:
        st.subheader(f"All tasks ({len(_tasks)})")
        if not _tasks:
            st.info("No tasks yet. Add one using the form on the right.")
        else:
            # Group by pet for readability
            _pet_map = {p.id: p for p in _pets}
            _shown: set[str] = set()
            for _pet in _pets:
                _pet_tasks = sorted(
                    [t for t in _tasks if t.pet_id == _pet.id],
                    key=lambda t: t.due_at,
                )
                if _pet_tasks:
                    st.markdown(f"##### {_pet.name}")
                    for _task in _pet_tasks:
                        _shown.add(_task.id)
                        _render_task_card(_task, _sys)
            # Orphaned tasks (pet deleted mid-session)
            _orphans = [t for t in _tasks if t.id not in _shown]
            if _orphans:
                st.markdown("##### ⚠️ Orphaned tasks")
                for _task in _orphans:
                    _render_task_card(_task, _sys)

    with col_form:
        st.subheader("Add a task")
        if not _pets:
            st.warning("Add at least one pet before adding tasks.")
        else:
            with st.form("add_task_form", clear_on_submit=True):
                _pet_options = {p.name: p.id for p in _pets}
                _t_pet  = st.selectbox("Pet *", list(_pet_options.keys()))
                _t_type = st.selectbox(
                    "Task type *",
                    ["feeding", "walk", "medication", "appointment"],
                    format_func=lambda x: _TASK_TYPE_LABELS.get(x, x),
                )

                _t_date = st.date_input(
                    "Due date", value=injected_now.date(), key="form_task_date",
                )
                _t_time = st.time_input(
                    "Due time",
                    value=injected_now.time().replace(second=0, microsecond=0),
                    key="form_task_time",
                    step=300,
                )
                _t_due = datetime.combine(_t_date, _t_time)
                _t_priority = st.slider("Priority (1 = low, 5 = critical)", 1, 5, 2)

                st.markdown("**Type-specific fields**")

                # Feeding
                _t_portion: float | None  = None
                # Walk
                _t_duration: int | None   = None
                # Medication
                _t_med_name: str | None   = None
                _t_dose: float | None     = None
                # Appointment
                _t_vet: str | None        = None
                _t_location: str | None   = None

                if _t_type == "feeding":
                    _t_portion  = st.number_input(
                        "Portion size (cups)", min_value=0.1, value=1.0, step=0.25, format="%.2f",
                    )
                elif _t_type == "walk":
                    _t_duration = st.number_input(
                        "Duration (minutes)", min_value=1, value=20, step=5,
                    )
                elif _t_type == "medication":
                    _t_med_name = st.text_input("Medication name *")
                    _t_dose     = st.number_input(
                        "Dose (units)", min_value=0.1, value=1.0, step=0.5, format="%.1f",
                    )
                elif _t_type == "appointment":
                    _t_vet      = st.text_input("Vet / provider name")
                    _t_location = st.text_input("Location *")

                if st.form_submit_button("Add task", type="primary", use_container_width=True):
                    _pet_id = _pet_options[_t_pet]
                    try:
                        if _t_type == "feeding":
                            _new_task: Task = FeedingTask(
                                pet_id=_pet_id, due_at=_t_due, priority=_t_priority,
                                metadata={"portion_size": _t_portion},
                            )
                        elif _t_type == "walk":
                            _new_task = WalkTask(
                                pet_id=_pet_id, due_at=_t_due, priority=_t_priority,
                                metadata={"duration_minutes": _t_duration},
                            )
                        elif _t_type == "medication":
                            if not _t_med_name or not _t_med_name.strip():
                                st.error("Medication name is required.")
                                st.stop()
                            _new_task = MedicationTask(
                                pet_id=_pet_id, due_at=_t_due, priority=_t_priority,
                                metadata={"medication_name": _t_med_name.strip(), "dose": _t_dose},
                            )
                        else:  # appointment
                            if not _t_location or not _t_location.strip():
                                st.error("Location is required for appointments.")
                                st.stop()
                            _new_task = AppointmentTask(
                                pet_id=_pet_id, due_at=_t_due, priority=_t_priority,
                                metadata={
                                    "vet_name": (_t_vet or "").strip(),
                                    "location": _t_location.strip(),
                                },
                            )
                        _sys.add_task(_new_task)
                        st.rerun()
                    except TaskValidationError as exc:
                        st.error(f"Validation failed: {exc}")
                    except SchedulingConflict as exc:
                        st.error(f"Scheduling conflict: {exc}  — adjust the due time.")
                    except KeyError as exc:
                        st.error(f"Pet not found: {exc}")


# ── Tab 3: Today's Schedule ───────────────────────────────────────────────────

with tab_schedule:
    _sys = _system()

    st.subheader(f"Today's Schedule — {injected_now.strftime('%A, %B')} {injected_now.day} {injected_now.strftime('%Y')}")
    st.caption(
        f"Deterministic urgency-sorted view as of `{injected_now.strftime('%H:%M')}`  "
        f"•  Window: `{injected_now.strftime('%Y-%m-%d')} 00:00 – 23:59`"
    )

    try:
        _schedule = _sys.build_schedule(injected_now)

        _conflicts = _schedule.detect_conflicts()
        for _conflict in _conflicts:
            st.warning(f"⚠️ Conflict detected: {_conflict}")

        if not _schedule.tasks:
            st.info(
                "No pending tasks today.  \n"
                "• Add tasks in the **Tasks** tab, or  \n"
                "• Use **Seed demo data** in the sidebar, or  \n"
                "• Adjust the **Demo clock** if your tasks are on a different day."
            )
        else:
            _pending  = sum(1 for t in _schedule.tasks if t.status == TaskStatus.PENDING)
            _complete = sum(1 for t in _schedule.tasks if t.status == TaskStatus.COMPLETE)
            _skipped  = sum(1 for t in _schedule.tasks if t.status == TaskStatus.SKIPPED)

            _m1, _m2, _m3, _m4 = st.columns(4)
            _m1.metric("Total",    len(_schedule.tasks))
            _m2.metric("Pending",  _pending,  delta=f"-{_pending}"  if _pending  == 0 else None)
            _m3.metric("Complete", _complete, delta=f"+{_complete}" if _complete else None)
            _m4.metric("Skipped",  _skipped)

            st.divider()
            for _task in _schedule.tasks:
                _render_task_card(_task, _sys)

    except SchedulingConflict as exc:
        st.error(
            f"**Scheduling conflict:** {exc}\n\n"
            "Adjust task due times in the **Tasks** tab to resolve."
        )
    except Exception as exc:
        st.error(f"Unexpected error building schedule: {exc}")


# ── Tab 4: AI Plan ────────────────────────────────────────────────────────────

with tab_ai:
    _sys = _system()

    st.subheader("🤖 AI-Planned Day")

    # Status banner
    if not config.ENABLE_AI:
        st.warning(
            "AI planning is disabled (`ENABLE_AI=false`).  \n"
            "Set `ENABLE_AI=true` in your `.env` and restart the app."
        )
    else:
        _info_col, _model_col = st.columns([3, 1])
        with _info_col:
            st.info(
                f"Model: `{config.PAWPAL_MODEL}` at `{config.OLLAMA_HOST}`  \n"
                f"Make sure **`ollama serve`** is running before generating a plan."
            )
        with _model_col:
            st.metric("RAG", "ON ✅" if config.ENABLE_RAG else "OFF ❌")

    if st.button("✨ Generate AI plan", type="primary"):
        with st.spinner(f"Asking `{config.PAWPAL_MODEL}` to plan the day…"):
            try:
                _plan = _sys.plan_ai_day(injected_now)
                st.session_state["last_plan"]     = _plan
                st.session_state["last_plan_now"] = injected_now
            except Exception as exc:
                st.error(f"Unexpected error during AI planning: {exc}")

    _plan = st.session_state.get("last_plan")

    if _plan is None:
        st.info("No plan generated yet. Click **✨ Generate AI plan** above.")
    else:
        _plan_now = st.session_state.get("last_plan_now", injected_now)

        # Plan status badge
        if _plan.is_ai_planned:
            st.success("✅ AI-planned and validated — showing AI ordering with suggested times.")
        else:
            st.warning(
                "⚠️ AI fallback active — Ollama was unreachable or the plan failed validation.  \n"
                "Showing the deterministic schedule instead."
            )

        # Flagged risks (full-width, above columns)
        if _plan.flagged_risks:
            with st.expander(
                f"⚠️ Flagged risks ({len(_plan.flagged_risks)})",
                expanded=True,
            ):
                for _risk in _plan.flagged_risks:
                    st.markdown(f"- {_risk}")

        # Side-by-side: AI plan | Deterministic schedule
        _col_ai, _col_det = st.columns(2, gap="large")

        with _col_ai:
            _badge = "🤖 AI Plan" if _plan.is_ai_planned else "📋 Fallback Plan"
            st.markdown(f"### {_badge}")
            st.caption(f"{len(_plan.ordered_tasks)} task(s) ordered by AI")

            for _i, _task in enumerate(_plan.ordered_tasks, 1):
                _rationale     = _plan.rationales[_i - 1] if _i - 1 < len(_plan.rationales) else ""
                _suggested_t   = _plan.time_windows.get(_task.id, "")
                _render_task_card(
                    _task, _sys,
                    show_complete_btn=False,
                    rationale=_rationale,
                    suggested_time=_suggested_t,
                    rank=_i,
                )

        with _col_det:
            st.markdown("### 📅 Deterministic Schedule")
            st.caption("For comparison")
            try:
                _det = _sys.build_schedule(_plan_now)
                if not _det.tasks:
                    st.info("No tasks in the deterministic schedule for this window.")
                else:
                    for _task in _det.tasks:
                        _tkey  = _task_type_key(_task)
                        _label = _TASK_TYPE_LABELS.get(_tkey, _tkey)
                        _badge = _STATUS_BADGES.get(_task.status, "")
                        _pname = _pet_name(_sys, _task.pet_id)
                        st.markdown(
                            f"{_badge} **{_label}** — {_pname}  "
                            f"`{_task.due_at.strftime('%H:%M')}`"
                        )
            except SchedulingConflict as exc:
                st.warning(f"Could not build deterministic schedule: {exc}")
            except Exception as exc:
                st.error(f"Unexpected error: {exc}")

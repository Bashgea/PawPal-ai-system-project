# CLAUDE.md — PawPal+

> Operating manual for working in this repo. Read this before making non-trivial changes.

---

## 1. Project overview

**PawPal+** is a Python application for managing pet care: feedings, walks, medications, and vet appointments. It models tasks as first-class objects, schedules them with priority/urgency rules, and uses a **local LLM via Ollama** to plan, validate, and repair daily care plans. The primary interface is a **Streamlit UI**; a `demo.py` script covers scripted runs and testing.

**In scope**
- Track pets and their care tasks (feeding, walking, medication, appointments).
- Algorithmic scheduling with prioritization (overdue, time-sensitive, dependency-aware).
- Local persistence of pets/tasks/history (JSON by default, SQLite optional).
- An **agentic AI workflow** (plan → validate → repair) running on a local Ollama model.
- Optional RAG over a small `knowledge/` corpus (breed care notes, medication guidelines).
- Streamlit UI for interactive use; `demo.py` for scripted end-to-end demonstration.

**Non-goals**
- No CLI interface — `demo.py` and Streamlit cover all use cases without the overhead.
- Not a veterinary diagnosis tool. No medical decisions.
- No multi-user accounts, auth, or cloud sync.
- No mobile app, push notifications, or calendar integrations (yet).
- Not a general-purpose task manager — pet-care semantics are baked in.
- No hosted-API support in the initial version. Ollama only.

---

## 2. Repository map

```
pawpal-plus/
├── pawpal_system.py          # Core domain + scheduler (main module)
├── ai/
│   ├── __init__.py
│   ├── agent.py              # Plan → validate → repair loop
│   ├── prompts.py            # Versioned prompt templates
│   ├── rag.py                # Lightweight retrieval over knowledge/
│   └── ollama_client.py      # Ollama wrapper (retries, timeouts, JSON mode)
├── knowledge/                # Small RAG corpus (.md files)
│   ├── feeding_guidelines.md
│   ├── medication_safety.md
│   └── breed_notes.md
├── demo.py                   # Scripted end-to-end demo (primary run entrypoint)
├── streamlit_app.py          # Streamlit UI (main interactive interface)
├── storage.py                # Load/save state (JSON or SQLite)
├── config.py                 # Env loading, constants, feature flags
├── logging_setup.py          # Central logging config
├── tests/
│   ├── test_scheduler.py
│   ├── test_tasks.py
│   ├── test_agent.py         # Uses a mocked Ollama client
│   └── test_eval.py
├── eval/
│   ├── cases.json            # Eval scenarios + expected invariants
│   └── run_eval.py           # Plan validity, repair success, fallback rate
├── .env.example
├── requirements.txt
├── README.md
└── CLAUDE.md
```

**One-line responsibilities:**
- `pawpal_system.py` — domain models, scheduler, public API used by UI/agent/demo.
- `ai/agent.py` — orchestrates plan → validate → repair; the **only** module that calls the model.
- `ai/ollama_client.py` — HTTP wrapper around Ollama; retries, timeouts, JSON-mode requests.
- `ai/rag.py` — retrieves snippets from `knowledge/` to ground prompts.
- `demo.py` — scripted scenario (hardcoded pets + tasks); proves the system works end-to-end.
- `streamlit_app.py` — interactive UI; add pets/tasks, view schedules, trigger AI plans.
- `storage.py` — pure I/O; no business logic.
- `config.py` — single source of truth for env vars and feature flags.

---

## 3. Architecture

### Core classes (in `pawpal_system.py`)

- **`Pet`** — `id`, `name`, `species`, `breed`, `weight`, `notes`.
- **`Task` (ABC)** — `id`, `pet_id`, `due_at`, `priority`, `status`, `metadata`.
  - Subclasses: `FeedingTask`, `WalkTask`, `MedicationTask`, `AppointmentTask`.
  - Each implements `urgency_score(now)` and `validate()`.
- **`Schedule`** — collection of tasks for a window; supports filtering, sorting, conflict detection.
- **`Scheduler`** — given pets + tasks + `now`, returns an ordered `Schedule`. Pure; no I/O.
- **`PawPalSystem`** — façade tying `Pet`s, `Task`s, `storage`, and `Scheduler` together. Streamlit/demo/agent talk only to this.

### Data flow

```
demo.py / streamlit_app.py
      │
      ▼
PawPalSystem ──► Scheduler ──► Schedule
      │                            │
      │                            ▼
      └────────► ai.agent.plan_day(schedule, context)
                       │
                       ├─► ai.rag.retrieve(query)            (optional)
                       ├─► ai.ollama_client.complete(prompt) (JSON mode, retries)
                       ├─► validate(plan, invariants)
                       └─► repair(plan, violations)          (bounded loop)
                              │
                              ▼
                       Validated plan → returned to caller
```

Storage is loaded once at startup, mutated through `PawPalSystem` methods, and saved after each state change.

---

## 4. Running the project

### Setup

```bash
# 1. Install Ollama (one-time, host machine)
#    https://ollama.com/download
ollama pull llama3.1:8b        # or qwen2.5:7b, mistral:7b, etc.
ollama serve                   # leave running in a separate terminal

# 2. Python project
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### demo.py — scripted run

`demo.py` is the fastest way to verify the full system works. It seeds a realistic scenario (2 pets, mixed task types) and runs the AI agent, printing the validated plan to stdout.

```bash
python demo.py              # full run with AI agent
python demo.py --no-ai      # deterministic schedule only (Ollama not required)
```

Use `demo.py` to:
- Confirm Ollama is working end-to-end.
- Smoke-test after changing prompts, the agent, or the scheduler.
- Show the system to someone quickly without opening a browser.

### Streamlit UI — interactive interface

```bash
streamlit run streamlit_app.py
```

The UI is the primary way to interact with PawPal+ beyond the demo. It covers:
- Adding and editing pets and tasks.
- Viewing today's deterministic schedule.
- Triggering an AI-planned day and inspecting the plan with rationales.
- Marking tasks complete.

Streamlit state is session-scoped. Persistent state lives in `PAWPAL_DB`; the UI reads and writes through `PawPalSystem`.

---

## 5. AI integration

### Feature: agentic plan → validate → repair (local Ollama)

This is **integrated, not a side demo**: `demo.py` and the Streamlit "Today's Plan" view both go through the agent.

### Pipeline

1. **Plan.** Given the deterministic `Schedule` + pet context + retrieved knowledge snippets, the model returns a structured JSON plan: ordered tasks, time windows, rationales, and flagged risks (e.g., medication-feeding interactions).
2. **Validate.** A pure-Python validator checks invariants:
   - All scheduled tasks are present; no extras invented.
   - Medication times respect minimum spacing and food/empty-stomach constraints.
   - No two walks within a configurable cooldown window.
   - Times fall within the requested day.
3. **Repair.** If validation fails, violations are summarized and the model is asked to fix *only* those issues. Bounded by `MAX_REPAIR_ITERS` (default 2). If still invalid → fall back to the deterministic schedule and log violations.

### Ollama specifics

- All model calls go through `ai/ollama_client.py`. No other module imports `ollama` or `httpx` for model I/O.
- Use Ollama's `format: "json"` mode for plan and repair calls. Smaller local models drift to prose otherwise.
- Default model: `llama3.1:8b`. Configurable via `PAWPAL_MODEL`.
- First call after `ollama serve` is slow (model load). The client logs a warning and proceeds; consider warming the model in `demo.py`.

### Prompt & tooling boundaries

- Prompts live in `ai/prompts.py` as named, versioned constants (`PLAN_PROMPT_V1`, `REPAIR_PROMPT_V1`). Do not inline prompt strings elsewhere.
- The model never touches storage. It returns JSON; `agent.py` applies it through `PawPalSystem`.
- RAG is read-only over `knowledge/*.md`. No web fetches.

### Failure modes & graceful degradation

- **Ollama not running** (connection refused on `OLLAMA_HOST`) → log a clear hint (`run \`ollama serve\``), fall back to deterministic schedule.
- **Model not pulled** (404 from Ollama) → log the missing model name and the `ollama pull` command, fall back.
- **Malformed JSON** → one re-ask with stricter format reminder, then fallback.
- **Validator never passes** → return last valid candidate or the deterministic schedule. Never return an invalid plan.
- **Timeout** → bounded by `MODEL_TIMEOUT_S`; retries with exponential backoff up to `MODEL_MAX_RETRIES`.

---

## 6. Configuration & environment variables

All env access goes through `config.py`. `.env.example` is the canonical list:

```env
# Ollama
OLLAMA_HOST=http://localhost:11434
PAWPAL_MODEL=llama3.1:8b
MODEL_TIMEOUT_S=60          # local models are slower; cold-load can be >10s
MODEL_MAX_RETRIES=2
MAX_REPAIR_ITERS=2

# Features
ENABLE_AI=true
ENABLE_RAG=true
KNOWLEDGE_DIR=./knowledge

# Storage & logging
PAWPAL_DB=./pawpal.json     # or sqlite:///pawpal.db
LOG_LEVEL=INFO
LOG_FILE=./pawpal.log
```

Rules:
- Never read `os.environ` outside `config.py`.
- `.env` is gitignored; `.env.example` is committed and kept in sync.
- No API keys are required or accepted in this version.

---

## 7. Running tests / evaluation / quality checks

```bash
pytest -q                          # unit + integration tests (mocked Ollama)
pytest tests/test_agent.py -q      # agent behavior
python eval/run_eval.py            # reliability eval, prints pass rate
ruff check . && ruff format --check .
mypy pawpal_system.py ai/
```

### Eval (`eval/run_eval.py`)

Runs the agent against `eval/cases.json` and reports:
- **Plan validity rate** (first try).
- **Repair success rate** (passes after repair).
- **Fallback rate** (deterministic schedule used).
- **Average repair iterations.**

Prompt or agent changes must include a before/after eval run in the PR. Regressions need an explanation.

### Test isolation

- Tests use a **mocked Ollama client** by default — fast, deterministic, no network.
- Set `PAWPAL_TEST_LIVE=1` to run against a real local Ollama instance (off in CI).
- Inject `now` into anything time-dependent. Never call `datetime.now()` inside domain logic.

---

## 8. Coding standards for this repo

- **Python 3.11+**. Modern typing (`list[str]`, `X | None`).
- **OOP-first** for the domain. Pure functions for the scheduler and validators.
- **Type hints required** on all public functions/methods. `mypy` expected to pass on `pawpal_system.py` and `ai/`.
- **Docstrings**: Google-style. One-liner minimum on every public class/method; full docstring (Args/Returns/Raises) on anything in `pawpal_system.py` or `ai/agent.py`.
- **Naming**: classes `PascalCase`, functions/vars `snake_case`, constants `UPPER_SNAKE`. Task subclasses end in `Task`.
- **Logging**: use `logging.getLogger(__name__)`; never `print()` outside `demo.py`. Levels:
  - `DEBUG` — prompts, retrieved snippets, raw model output.
  - `INFO` — high-level flow (plan requested, repair attempt N, fallback engaged).
  - `WARNING` — degraded mode (Ollama unreachable, JSON re-ask).
  - `ERROR` — caught exceptions with context.
- **Exceptions**: define domain errors in `pawpal_system.py` (`TaskValidationError`, `SchedulingConflict`) and AI errors in `ai/agent.py` (`PlanInvalidError`, `ModelUnavailableError`). Catch narrowly; never bare `except`. `streamlit_app.py` and `demo.py` are the only layers that convert exceptions to user-facing messages.
- **No I/O in domain code.** `Scheduler`, `Task`, validators stay pure.
- **Determinism in tests.** Inject `now`. Mock the Ollama client.

---

## 9. Common tasks

### Add a new task type (e.g., `GroomingTask`)
1. Subclass `Task` in `pawpal_system.py`. Implement `urgency_score(now)` and `validate()`.
2. Register the type in the task factory (`Task.from_dict`).
3. Add the new task type to the Streamlit form inputs in `streamlit_app.py`.
4. Update the JSON schema in `ai/prompts.py` so the agent knows the new type.
5. Add unit tests in `tests/test_tasks.py` and at least one eval case.

### Add a scheduling rule (e.g., "no walk within 30 min of feeding")
1. Implement as a pure function in `pawpal_system.py` (or `rules.py` if rules grow).
2. Wire into `Scheduler.apply_rules`.
3. Mirror the rule in the agent **validator** so model plans must also satisfy it.
4. Test: construct a violating schedule, assert the rule fires.
5. Add an eval case the model is likely to violate, to exercise repair.

### Add a knowledge document for RAG
1. Drop a focused `.md` file into `knowledge/`. Keep under ~2KB; split if larger.
2. No code change needed — `ai/rag.py` indexes the directory at startup.
3. If retrieval quality matters, add a test in `test_agent.py` asserting the snippet is retrieved for a representative query.

### Change a prompt
1. Bump the version constant (`PLAN_PROMPT_V1` → `_V2`); keep the old one until eval passes.
2. Run `python eval/run_eval.py` before and after; include both numbers in the PR.

### Switch to a different Ollama model
1. `ollama pull <model>` on the host.
2. Set `PAWPAL_MODEL=<model>` in `.env`.
3. Run `python eval/run_eval.py` — different models have different JSON adherence and pass rates.

---

# CLAUDE.md — PawPal+

> Operating manual for working in this repo. Read this before making non-trivial changes.

---

## 1. Project overview

**PawPal+** is a Python application for managing pet care: feedings, walks, medications, and vet appointments. It models tasks as first-class objects, schedules them with priority/urgency rules, and uses a **local LLM via Ollama** to plan, validate, and repair daily care plans. The primary interface is a **Streamlit UI** (`streamlit run streamlit_app.py`).

**In scope**
- Track pets and their care tasks (feeding, walking, medication, appointments).
- Algorithmic scheduling with prioritization (overdue, time-sensitive, dependency-aware).
- Local persistence of pets/tasks/history (JSON by default, SQLite optional).
- An **agentic AI workflow** (plan → validate → repair) running on a local Ollama model.
- Optional RAG over a small `knowledge/` corpus (breed care notes, medication guidelines).
- **Streamlit UI as the primary interactive interface** — add pets/tasks, view schedule, trigger AI plans.

**Non-goals**
- No CLI interface — Streamlit is the interface for all interactive use.
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
│   ├── rag.py                # (not yet implemented) RAG over knowledge/
│   └── ollama_client.py      # Ollama wrapper (retries, timeouts, JSON mode)
├── knowledge/                # (not yet implemented) RAG corpus (.md files)
├── streamlit_app.py          # Streamlit UI — primary run entrypoint
├── demo.py                   # (not yet implemented) optional scripted smoke-test
├── storage.py                # Load/save state (JSON or SQLite)
├── config.py                 # Env loading, constants, feature flags
├── logging_setup.py          # Central logging config
├── tests/
│   ├── test_scheduler.py
│   ├── test_agent.py         # Uses a mocked Ollama client
│   ├── test_ollama_client.py
│   └── test_storage.py
├── eval/                     # (not yet implemented) reliability eval harness
├── .env.example
├── requirements.txt
├── README.md
└── CLAUDE.md
```

**One-line responsibilities:**
- `pawpal_system.py` — domain models, scheduler, public API used by UI/agent/demo.
- `ai/agent.py` — orchestrates plan → validate → repair; the **only** module that calls the model.
- `ai/ollama_client.py` — HTTP wrapper around Ollama; retries, timeouts, JSON-mode requests.
- `ai/rag.py` — *(not yet implemented)* will retrieve snippets from `knowledge/` to ground prompts; the agent falls back gracefully when absent.
- `streamlit_app.py` — **primary entry point**; add pets/tasks, view schedule, trigger AI plans.
- `demo.py` — *(not yet implemented)* planned scripted scenario for quick smoke-tests.
- `storage.py` — pure I/O; no business logic.
- `config.py` — single source of truth for env vars and feature flags (loads `.env` via python-dotenv).
- `logging_setup.py` — central logging configuration; call `configure()` at app startup.

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
streamlit_app.py
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
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # edit PAWPAL_MODEL, OLLAMA_HOST, etc. as needed
```

### Streamlit UI — primary interface

```bash
streamlit run streamlit_app.py
```

This is the normal way to use PawPal+. The UI covers:
- Adding and editing pets and tasks (Pets and Tasks tabs).
- Viewing today's urgency-sorted deterministic schedule with mark-complete buttons.
- Triggering an AI-planned day and comparing it side-by-side with the deterministic schedule.
- **Seed demo data** button: populates Rex (dog) + Luna (cat) with 5 mixed tasks in one click.
- **Demo clock**: inject a custom `now` datetime for reproducible class demos.
- **Reset DB**: wipe all data and start fresh (requires checkbox confirmation).

Streamlit state is session-scoped. Persistent state lives in `PAWPAL_DB`; the UI reads and writes exclusively through `PawPalSystem`.

### demo.py — not yet implemented

A scripted `demo.py` is planned as an optional developer shortcut (seeds hardcoded pets/tasks, runs the agent, prints results to stdout). It does not exist yet. Use the Streamlit **Seed demo data** button and **AI Plan** tab for the same workflow in the meantime.

---

## 5. AI integration

### Feature: agentic plan → validate → repair (local Ollama)

This is **integrated, not a side feature**: the Streamlit "AI Plan" tab goes through the full agent pipeline.

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
- First call after `ollama serve` is slow (model load). The client logs a warning and proceeds. The Streamlit spinner gives visible feedback during warm-up.

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
ruff check . && ruff format --check .
mypy pawpal_system.py ai/
```

### Eval (`eval/run_eval.py`) — not yet implemented

The eval harness (`eval/`) is planned but not yet implemented. When added, it will run the agent against `eval/cases.json` and report plan validity rate, repair success rate, fallback rate, and average repair iterations. Prompt changes should include a before/after eval run once the harness exists.

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
- **Logging**: use `logging.getLogger(__name__)`; never `print()` in core modules (domain, AI, storage). Streamlit display APIs are the correct output mechanism in `streamlit_app.py`. Levels:
  - `DEBUG` — prompts, retrieved snippets, raw model output.
  - `INFO` — high-level flow (plan requested, repair attempt N, fallback engaged).
  - `WARNING` — degraded mode (Ollama unreachable, JSON re-ask).
  - `ERROR` — caught exceptions with context.
- **Exceptions**: define domain errors in `pawpal_system.py` (`TaskValidationError`, `SchedulingConflict`) and AI errors in `ai/agent.py` (`PlanInvalidError`, `ModelUnavailableError`). Catch narrowly; never bare `except`. `streamlit_app.py` is the only layer that converts exceptions to user-facing messages.
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

### Add a knowledge document for RAG *(requires implementing ai/rag.py first)*
1. Drop a focused `.md` file into `knowledge/`. Keep under ~2KB; split if larger.
2. Implement `ai/rag.py` with a `RAG` class that exposes `retrieve(query) -> list[str]`.
3. No further change needed in `agent.py` — it already imports `RAG` inside a try/except.
4. If retrieval quality matters, add a test in `test_agent.py` asserting the snippet is retrieved for a representative query.

### Change a prompt
1. Bump the version constant (`PLAN_PROMPT_V1` → `_V2`); keep the old one until testing confirms the new version works correctly.

### Switch to a different Ollama model
1. `ollama pull <model>` on the host.
2. Set `PAWPAL_MODEL=<model>` in `.env`.
3. Verify the AI Plan tab works end-to-end in the Streamlit UI.

---

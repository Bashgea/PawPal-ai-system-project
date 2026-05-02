# PawPal+ — Architecture Diagrams

> Edit this file freely. If a diagram renders incorrectly, paste the code block into
> https://mermaid.live for live preview and editing.

---

## Assumptions

- `plan_ai_day(now)` is the inferred public method name on `PawPalSystem` that triggers the
  agent; actual name may differ.
- `PlanResult` is treated as a DTO (dataclass or TypedDict); fields are inferred from the
  JSON plan description in CLAUDE.md §5.
- `Agent`'s `validate` and `repair` are private methods — CLAUDE.md implies no separate
  Validator class.
- `OllamaClient` exposes `complete_json(prompt)` for JSON-mode and `complete(prompt)` for
  plain calls (inferred from "JSON mode for plan and repair calls").
- `RAG` is a class that indexes `knowledge/` at instantiation; `retrieve(query)` signature
  is inferred from the data-flow description.
- `Scheduler.apply_rules` is explicitly named in CLAUDE.md §9.
- `config.py`, `storage.py`, and `logging_setup.py` are module-level utilities with no
  named classes; present in the component diagram only.
- The malformed-JSON re-ask (an `OllamaClient` detail) is collapsed into the
  "Ollama error" alt branch in the sequence diagram for readability.

---

## Diagram 1 — Class Diagram

```mermaid
classDiagram
    class Pet {
        +str id
        +str name
        +str species
        +str breed
        +float weight
        +str notes
    }

    class Task {
        <<abstract>>
        +str id
        +str pet_id
        +datetime due_at
        +int priority
        +str status
        +dict metadata
        +urgency_score(now) float*
        +validate() list~str~*
        +from_dict(data) Task$
    }

    class FeedingTask {
        +urgency_score(now) float
        +validate() list~str~
    }

    class WalkTask {
        +urgency_score(now) float
        +validate() list~str~
    }

    class MedicationTask {
        +urgency_score(now) float
        +validate() list~str~
    }

    class AppointmentTask {
        +urgency_score(now) float
        +validate() list~str~
    }

    class Schedule {
        +list~Task~ tasks
        +datetime window_start
        +datetime window_end
        +filter(criteria) Schedule
        +sort_by_urgency() Schedule
        +detect_conflicts() list~SchedulingConflict~
    }

    class Scheduler {
        +build(pets, tasks, now) Schedule
        +apply_rules(schedule) Schedule
    }

    class PawPalSystem {
        +add_pet(pet) Pet
        +get_pets() list~Pet~
        +add_task(task) Task
        +get_tasks(pet_id) list~Task~
        +build_schedule(now) Schedule
        +mark_complete(task_id) None
        +plan_ai_day(now) PlanResult
    }

    class Agent {
        <<orchestrator>>
        +plan_day(schedule, context) PlanResult
        -validate(plan, invariants) list~str~
        -repair(plan, violations) PlanResult
    }

    class OllamaClient {
        <<http-client>>
        +complete_json(prompt) dict
        +complete(prompt) str
    }

    class RAG {
        <<retriever>>
        +retrieve(query) list~str~
    }

    class PlanResult {
        <<DTO>>
        +list ordered_tasks
        +dict time_windows
        +list~str~ rationales
        +list~str~ flagged_risks
        +bool is_ai_planned
    }

    Task <|-- FeedingTask
    Task <|-- WalkTask
    Task <|-- MedicationTask
    Task <|-- AppointmentTask

    PawPalSystem "1" *-- "many" Pet : owns
    PawPalSystem "1" *-- "many" Task : owns
    PawPalSystem --> Scheduler : uses
    PawPalSystem --> Agent : calls
    Scheduler --> Schedule : produces
    Schedule "1" o-- "many" Task : aggregates
    Agent --> OllamaClient : calls
    Agent --> RAG : retrieves
    Agent ..> PlanResult : returns

    note for Agent "ai/agent.py — only module that calls the model"
    note for OllamaClient "ai/ollama_client.py — retries, timeouts, JSON mode"
    note for RAG "ai/rag.py — indexes knowledge/ at startup"
```

---

## Diagram 2 — Component / Package Diagram

```mermaid
flowchart TB
    subgraph entry["Entry Points"]
        SA[streamlit_app.py]
        DM[demo.py]
    end

    subgraph core["Core Domain"]
        PS[pawpal_system.py]
    end

    subgraph support["Support"]
        ST[storage.py]
        CF[config.py]
    end

    subgraph ai_pkg["ai/"]
        AG[agent.py]
        OC[ollama_client.py]
        RG[rag.py]
        PR[prompts.py]
    end

    subgraph kb["knowledge/"]
        KD[(corpus — *.md)]
    end

    DB[(pawpal.json / SQLite)]
    OL([Ollama :11434])

    SA -->|calls| PS
    DM -->|calls| PS
    PS -->|calls| AG
    PS -->|reads/writes| ST
    PS -->|reads| CF
    OC -->|reads| CF
    ST -->|reads/writes| DB
    AG -->|calls| OC
    AG -->|reads| PR
    AG -.->|optional calls| RG
    RG -->|reads| KD
    OC -->|HTTP JSON| OL
```

---

## Diagram 3 — Sequence: AI-Planned Day

```mermaid
sequenceDiagram
    actor U as demo.py or UI
    participant PS as PawPalSystem
    participant SC as Scheduler
    participant AG as Agent
    participant RG as RAG
    participant OC as OllamaClient
    participant OL as Ollama

    U->>PS: plan_ai_day(now)
    PS->>SC: build(pets, tasks, now)
    SC-->>PS: deterministic Schedule

    PS->>AG: plan_day(schedule, context)

    opt ENABLE_RAG=true
        AG->>RG: retrieve(query)
        RG-->>AG: knowledge snippets
    end

    AG->>OC: complete_json(PLAN_PROMPT_V1)
    OC->>OL: POST /api/generate format=json

    alt Ollama unreachable or model not pulled
        OC-->>AG: ModelUnavailableError
        AG-->>PS: signal fallback
        PS-->>U: deterministic Schedule + warning
    else JSON plan received
        OL-->>OC: raw JSON
        OC-->>AG: plan dict
        AG->>AG: validate(plan, invariants)

        loop 0..MAX_REPAIR_ITERS while violations exist
            AG->>OC: complete_json(REPAIR_PROMPT_V1 + violations)
            OC->>OL: POST /api/generate format=json
            OL-->>OC: repaired JSON
            OC-->>AG: repaired dict
            AG->>AG: validate(repaired, invariants)
        end

        alt plan valid
            AG-->>PS: PlanResult
            PS-->>U: PlanResult (AI-planned)
        else still invalid after max repairs
            AG-->>PS: signal fallback
            PS-->>U: deterministic Schedule + violations logged
        end
    end
```

---

## Design Notes

- **Hard boundary at the facade.** Both entry points (`streamlit_app.py`, `demo.py`) and
  `Agent` talk only to `PawPalSystem`. Neither Scheduler, storage, nor the Ollama client
  is accessed from outside its designated layer.
- **Single responsibility per module.** `Scheduler` and all `Task` subclasses are pure
  (no I/O, no randomness); `storage.py` is pure I/O with no business logic; `config.py`
  is the only place that reads `os.environ`; `agent.py` is the only place that calls
  the model.
- **Correctness enforced by the Python validator, not the LLM.** The validator checks hard
  invariants (medication spacing, walk cooldown, day boundaries). LLM output is accepted
  only after passing validation — or is repaired until it does. An invalid plan is never
  returned.
- **Deterministic fallback is always available.** `Scheduler → Schedule` runs before any
  AI call and is returned whenever the agent path fails, so the app is never blocked by
  Ollama availability.
- **Prompt versioning protects eval baselines.** Prompts are named constants
  (`PLAN_PROMPT_V1`, `REPAIR_PROMPT_V1`) in `ai/prompts.py`. The old version is kept
  until `eval/run_eval.py` confirms the new version does not regress.
- **RAG is additive and gated.** Retrieval augments prompts but has no write path and can
  be disabled via `ENABLE_RAG=false` without changing any other module's behavior.

# PawPal+

PawPal+ is a Python application for managing pet care — feedings, walks, medications, and vet appointments. It uses an urgency-aware scheduler to prioritize tasks and an optional local AI agent (via [Ollama](https://ollama.com)) to generate, validate, and repair daily care plans. The primary interface is a **Streamlit web UI**.

---

## Prerequisites

- **Python 3.11+**
- **Ollama** — install from [ollama.com/download](https://ollama.com/download), then:
  ```bash
  ollama pull llama3.1:8b   # or qwen2.5:7b, mistral:7b, etc.
  ollama serve              # keep running in a separate terminal
  ```

---

## Setup

**Windows**
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

**macOS / Linux**
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

After copying `.env`, open it and adjust at minimum:
- `PAWPAL_MODEL` — model name you pulled (default `llama3.1:8b`)
- `MODEL_TIMEOUT_S` — raise to `180` on slow hardware (default `120`)

---

## Run

```bash
python -m streamlit run streamlit_app.py
```

> **Windows note:** use `python -m streamlit` if `streamlit` is not on your `PATH`.

Open `http://localhost:8501`. The UI lets you add pets and tasks, view the urgency-sorted schedule, and trigger an AI-planned day.

Use **Seed demo data** in the sidebar to populate Rex (dog) + Luna (cat) with sample tasks in one click.

---

## Tests

```bash
pytest -q
```

Tests use a mocked Ollama client — no running Ollama instance required.

---

## Diagnostic

```bash
python -m ai.ollama_client
```

Checks proxy variables, `localhost` address resolution, Ollama reachability, and runs a minimal generate call. Useful when the AI plan times out or fails silently.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| AI plan times out | Raise `MODEL_TIMEOUT_S` in `.env` (cold model load from disk can take 60–120 s) |
| `Connection refused` on localhost | PawPal+ normalises `localhost` → `127.0.0.1` automatically to avoid the Windows IPv6 delay; verify that `ollama serve` is running |
| Proxy blocking Ollama | PawPal+ bypasses system/env proxies for local Ollama calls; run `python -m ai.ollama_client` to see detected proxy vars |
| Need to reset all data | Use **Reset DB** in the Streamlit sidebar (requires checkbox confirmation) |
| `pawpal.json` locked | Close the file in any editor and ensure no duplicate Streamlit processes are running |

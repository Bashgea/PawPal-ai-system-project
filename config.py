"""config.py — reads env vars once; all other modules import from here."""

import os

OLLAMA_HOST      = os.getenv("OLLAMA_HOST",    "http://localhost:11434")
PAWPAL_MODEL     = os.getenv("PAWPAL_MODEL",   "llama3.1:8b")
MODEL_TIMEOUT_S  = int(os.getenv("MODEL_TIMEOUT_S",   "60"))
MODEL_MAX_RETRIES = int(os.getenv("MODEL_MAX_RETRIES", "2"))
MAX_REPAIR_ITERS = int(os.getenv("MAX_REPAIR_ITERS",  "2"))

ENABLE_AI  = os.getenv("ENABLE_AI",  "true").lower() == "true"
ENABLE_RAG = os.getenv("ENABLE_RAG", "true").lower() == "true"

KNOWLEDGE_DIR = os.getenv("KNOWLEDGE_DIR", "./knowledge")
PAWPAL_DB     = os.getenv("PAWPAL_DB",     "./pawpal.json")
LOG_LEVEL     = os.getenv("LOG_LEVEL",     "INFO")
LOG_FILE      = os.getenv("LOG_FILE",      "./pawpal.log")

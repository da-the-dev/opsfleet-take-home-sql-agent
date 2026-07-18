"""Central configuration: env vars, dataset constants, safety budgets."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- Dataset -----------------------------------------------------------------
DATASET_ID = "bigquery-public-data.thelook_ecommerce"
TABLES = ["orders", "order_items", "products", "users"]

# PII source columns (design doc §4.2, layer 1). Lineage analysis rejects any
# query whose output derives from these, however aliased or transformed.
PII_COLUMNS: dict[str, set[str]] = {
    "users": {"email", "first_name", "last_name"},
}

# --- Models ------------------------------------------------------------------
# Provider selection: gemini (default) | ollama | openrouter. See llm.py.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "models/gemini-embedding-001")
# Local models via Ollama (needs a tool-calling-capable model pulled).
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
# OpenRouter: primary when LLM_PROVIDER=openrouter, otherwise automatic
# fallback whenever the key is set (design doc §4.5); empty = disabled.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
OPENROUTER_EMBEDDING_MODEL = os.getenv(
    "OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small"
)
# Embeddings for golden-trio retrieval: auto (follow LLM_PROVIDER) | gemini |
# ollama | openrouter | none (forces keyword-fallback retrieval).
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "auto").lower()

# --- Safety / cost budgets (design doc §4.5) ----------------------------------
MAX_BYTES_BILLED = int(os.getenv("MAX_BYTES_BILLED", str(2 * 1024**3)))  # 2 GiB
MAX_RESULT_ROWS = int(os.getenv("MAX_RESULT_ROWS", "200"))
DEFAULT_ROW_LIMIT = 100  # LIMIT injected into queries that lack one
MAX_SQL_ATTEMPTS = int(os.getenv("MAX_SQL_ATTEMPTS", "3"))

# --- Local state ---------------------------------------------------------------
STATE_DIR = Path(os.getenv("AGENT_STATE_DIR", PROJECT_ROOT / ".agent_state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DB = STATE_DIR / "reports.sqlite3"
CHECKPOINT_DB = STATE_DIR / "checkpoints.sqlite3"

PERSONA_FILE = PROJECT_ROOT / "persona.md"
TRIOS_FILE = PROJECT_ROOT / "data" / "golden_trios.json"

# BigQuery jobs against public datasets still bill (and are quota'd) to *your*
# project, so one must be set.
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")

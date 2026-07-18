"""Live consistency eval: same question, N fresh threads, verdicts must agree.

Runs the real agent against live BigQuery and the configured LLM provider —
costs a few cents and ~2 minutes per run. NOT part of the offline suite:

    uv run pytest evals -v                 # run it (3 runs by default)
    CONSISTENCY_RUNS=5 uv run pytest evals # bigger batch

Asserts the properties established in docs/CONSISTENCY_PROBE.md:
methodology pinning (LEFT JOIN + cancelled/returned excluded), numeric and
verdict agreement across runs, non-blank date-stamped answers, and no
unprompted report saves. Full transcripts land in evals/output/ for review.
"""

import json
import os
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver

pytestmark = pytest.mark.live

QUESTION = (
    "Why are users from Texas underspending compared to California? Dig into the drivers."
)
STATES = ("Texas", "California")
RUNS = int(os.getenv("CONSISTENCY_RUNS", "3"))
NUMERIC_TOLERANCE = 0.02  # thelook refreshes continuously; allow 2% drift mid-batch


def _text(content) -> str:
    if isinstance(content, list):
        return "".join(p if isinstance(p, str) else p.get("text", "") for p in content)
    return str(content)


@pytest.fixture(scope="module")
def batch():
    try:
        from data_agent.graph import Agent

        agent = Agent(SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)))
    except Exception as e:  # noqa: BLE001 - no creds/providers → skip, don't fail
        pytest.skip(f"live providers unavailable: {e}")

    runs = []
    for i in range(RUNS):
        cfg = {
            "configurable": {"user_id": "consistency_eval", "thread_id": f"eval-{i}"},
            "recursion_limit": 50,
        }
        res = agent.graph.invoke({"messages": [HumanMessage(QUESTION)]}, cfg)
        msgs = res["messages"]
        tool_calls = [
            c
            for m in msgs
            if isinstance(m, AIMessage)
            for c in (m.tool_calls or [])
        ]
        runs.append(
            {
                "sqls": [c["args"].get("sql", "") for c in tool_calls if c["name"] == "run_sql"],
                "saves": [c for c in tool_calls if c["name"] == "save_report"],
                "results": [
                    _text(m.content) for m in msgs if m.type == "tool" and m.name == "run_sql"
                ],
                "final": _text(msgs[-1].content),
            }
        )
        time.sleep(5)

    outdir = Path(__file__).parent / "output"
    outdir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    transcript = outdir / f"consistency-{stamp}.txt"
    with transcript.open("w") as f:
        f.write(f"QUESTION: {QUESTION}\nRUNS: {RUNS}\n")
        for i, run in enumerate(runs, 1):
            f.write(f"\n{'='*80}\nRUN {i}\n")
            for sql in run["sqls"]:
                f.write(f"  SQL: {' '.join(sql.split())[:500]}\n")
            f.write(f"\nFINAL:\n{run['final']}\n")
    print(f"\ntranscript: {transcript}")
    return runs


def _rev_per_customer(row: dict) -> float | None:
    """Derive revenue-per-customer from a state row, whatever the model named
    its columns (runs vary: customers/registered_users/active_customers…).
    Computed from raw revenue and the *largest* customer count (registered-user
    basis) so the denominator convention is comparable across runs."""
    revenue = None
    customers = []
    for key, value in row.items():
        if not isinstance(value, (int, float)):
            continue
        name = key.lower()
        if "per" in name:  # already-derived ratios: not raw inputs
            continue
        if revenue is None and ("revenue" in name or "spend" in name):
            revenue = float(value)
        if ("customer" in name or "user" in name) and value > 0:
            customers.append(float(value))
    if revenue is None or not customers:
        return None
    return revenue / max(customers)


def _primary_metrics(run) -> dict | None:
    """First result set from which revenue-per-customer is derivable for both states."""
    for raw in run["results"]:
        try:
            rows = json.loads(raw).get("rows", [])
        except (json.JSONDecodeError, AttributeError):
            continue
        vals = {}
        for row in rows:
            if isinstance(row, dict) and row.get("state") in STATES:
                derived = _rev_per_customer(row)
                if derived is not None:
                    vals[row["state"]] = derived
        if len(vals) == 2:
            return vals
    return None


def test_every_run_gives_a_real_answer(batch):
    for i, run in enumerate(batch, 1):
        assert run["final"].strip(), f"run {i}: blank final answer"
        for state in STATES:
            assert state in run["final"], f"run {i}: answer never mentions {state}"


def test_no_unprompted_saves(batch):
    for i, run in enumerate(batch, 1):
        assert not run["saves"], f"run {i}: saved a report without being asked"


def test_methodology_is_pinned(batch):
    """The golden-trio conventions must appear in every run's comparison SQL."""
    for i, run in enumerate(batch, 1):
        # Primary comparison = first query naming both states that actually
        # joins tables (diagnostic probes like SELECT DISTINCT state don't count).
        comparison_sqls = [
            s
            for s in run["sqls"]
            if "Texas" in s and "California" in s and "JOIN" in s.upper()
        ]
        assert comparison_sqls, f"run {i}: no state-comparison join query found"
        primary = comparison_sqls[0].upper()
        assert "LEFT JOIN" in primary, f"run {i}: primary comparison lost LEFT JOIN"
        assert "CANCELLED" in primary, f"run {i}: primary comparison lost status filter"


def test_metrics_and_verdict_agree_across_runs(batch):
    metrics = [_primary_metrics(run) for run in batch]
    assert all(m is not None for m in metrics), (
        f"runs without a per-customer comparison result: "
        f"{[i for i, m in enumerate(metrics, 1) if m is None]}"
    )
    verdicts = [m["Texas"] > m["California"] for m in metrics]
    assert len(set(verdicts)) == 1, f"verdict flipped between runs: {metrics}"
    for state in STATES:
        values = [m[state] for m in metrics]
        spread = (max(values) - min(values)) / max(values)
        assert spread <= NUMERIC_TOLERANCE, (
            f"{state} revenue_per_customer varies {spread:.1%} across runs: {values}"
        )


def test_answers_are_date_stamped(batch):
    year = str(date.today().year)
    for i, run in enumerate(batch, 1):
        assert year in run["final"], f"run {i}: answer carries no current-date stamp"

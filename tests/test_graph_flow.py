"""Offline integration tests of the graph wiring.

A scripted fake LLM and a fake BigQuery runner drive the real graph through
its critical paths — self-correction after a guard rejection, layer-2/3
masking, retry-budget exhaustion, and the delete-confirmation interrupt —
with no network, credentials, or API quota.
"""

import sqlite3

from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import data_agent.graph as graph_mod
from data_agent.bq import QueryResult
from data_agent.graph import Agent
from data_agent.reports import ReportLibrary


class FakeRunner:
    """Stands in for BigQueryRunner; returns one canned result set."""

    def __init__(self, *a, **kw):
        self.executed: list[str] = []

    def execute(self, sql):
        self.executed.append(sql)
        return QueryResult(
            rows=[
                {"user_id": 42, "note": "reach me at vip@example.com", "spend": 999.0},
                {"user_id": 7, "note": "n/a", "spend": 500.0},
            ],
            total_rows=2,
            truncated=False,
            bytes_processed=1024,
        )

    def schema_context(self):
        return "CREATE TABLE users (...);"


class FakeLLM:
    """Returns scripted AIMessages in order; appends within a fixed final answer."""

    def __init__(self, script):
        self.script = list(script)

    def invoke(self, messages):
        return self.script.pop(0)


def make_agent(tmp_path, script, monkeypatch):
    monkeypatch.setattr(graph_mod, "BigQueryRunner", FakeRunner)
    monkeypatch.setattr(graph_mod, "ReportLibrary", lambda: ReportLibrary(tmp_path / "r.db"))
    monkeypatch.setattr(Agent, "_build_llm", lambda self: FakeLLM(script))
    checkpointer = SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    return Agent(checkpointer)


def run_config(user="manager_a", thread="t1"):
    return {"configurable": {"user_id": user, "thread_id": thread}}


def tool_call(name, args, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def test_self_correction_and_masking(tmp_path, monkeypatch):
    script = [
        # 1st attempt: violates PII policy → guard rejects, no execution
        tool_call("run_sql", {"sql": "SELECT email FROM `bigquery-public-data.thelook_ecommerce.users`"}),
        # 2nd attempt: clean query → fake rows (contain an email in a text cell)
        tool_call("run_sql", {"sql": "SELECT id AS user_id FROM `bigquery-public-data.thelook_ecommerce.users`"}, "c2"),
        # final answer tries to leak another email → layer 3 must catch it
        AIMessage(content="Top spender is user 42 (contact: leak@corp.io)."),
    ]
    agent = make_agent(tmp_path, script, monkeypatch)
    result = agent.graph.invoke({"messages": [("user", "who spends most?")]}, run_config())

    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert "rejected by policy" in tool_messages[0].content
    # Layer 2: the email inside result rows was masked before reaching the LLM
    assert "vip@example.com" not in tool_messages[1].content
    assert "<EMAIL_" in tool_messages[1].content
    # Exactly one real execution (the rejected query never ran)
    assert len(agent.runner.executed) == 1
    # Layer 3: final output scrubbed
    assert "leak@corp.io" not in result["messages"][-1].content
    assert "<EMAIL_" in result["messages"][-1].content


def test_retry_budget_exhaustion(tmp_path, monkeypatch):
    def bad(call_id):  # fresh object per attempt: same id would replace, not append
        return tool_call(
            "run_sql",
            {"sql": "SELECT first_name FROM `bigquery-public-data.thelook_ecommerce.users`"},
            call_id,
        )

    script = [bad("c1"), bad("c2"), bad("c3"),
              tool_call("run_sql", {"sql": "SELECT 1 AS over_budget"}, "c4"),
              AIMessage(content="I could not answer this; here is what I tried.")]
    agent = make_agent(tmp_path, script, monkeypatch)
    result = agent.graph.invoke({"messages": [("user", "emails please")]}, run_config())

    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert all("rejected by policy" in m.content for m in tool_messages[:3])
    assert "EXHAUSTED" in tool_messages[3].content  # 4th attempt refused by budget
    assert agent.runner.executed == []  # nothing ever reached BigQuery


def test_delete_requires_confirmation(tmp_path, monkeypatch):
    script = [
        tool_call("delete_reports", {"mentioning": "Client X"}),
        AIMessage(content="Done — both reports are deleted (recoverable for 30 days)."),
    ]
    agent = make_agent(tmp_path, script, monkeypatch)
    agent.library.save("manager_a", "Client X Q2", "body")
    agent.library.save("manager_a", "Client X Q3", "body")
    agent.library.save("manager_b", "Client X private", "body")  # other user's report

    result = agent.graph.invoke({"messages": [("user", "delete Client X reports")]}, run_config())
    assert "__interrupt__" in result
    preview = result["__interrupt__"][0].value
    assert len(preview["matches"]) == 2  # manager_b's report is invisible

    # Nothing deleted while paused
    assert len(agent.library.list_reports("manager_a")) == 2

    result = agent.graph.invoke(Command(resume="y"), run_config())
    assert len(agent.library.list_reports("manager_a")) == 0
    assert len(agent.library.list_reports("manager_b")) == 1  # untouched
    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert "Deleted 2" in tool_messages[-1].content


def test_delete_declined(tmp_path, monkeypatch):
    script = [
        tool_call("delete_reports", {"mentioning": "Q2"}),
        AIMessage(content="Okay — nothing was deleted."),
    ]
    agent = make_agent(tmp_path, script, monkeypatch)
    agent.library.save("manager_a", "Q2 review", "body")

    result = agent.graph.invoke({"messages": [("user", "delete my Q2 report")]}, run_config())
    assert "__interrupt__" in result
    result = agent.graph.invoke(Command(resume="no"), run_config())
    assert len(agent.library.list_reports("manager_a")) == 1
    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert "cancelled" in tool_messages[-1].content.lower()

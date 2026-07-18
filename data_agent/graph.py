"""The LangGraph agent (docs/ARCHITECTURE.md §1, agent graph).

Topology: agent → tools → agent … → finalize. Safety and resilience are
*structural* — they live in the tool boundary and graph nodes, not in prompt
text:

- ``run_sql`` internally runs validate (sqlguard) → dry-run → execute → mask
  (pii). The model only ever sees masked rows.
- SQL failures are counted in graph state; the retry budget
  (``MAX_SQL_ATTEMPTS``) is enforced here, not left to the model's judgment.
- ``delete_reports`` previews matches, then pauses the graph with
  ``interrupt()``; nothing is deleted until the user resumes with a
  confirmation. Delete calls are dispatched before any other tool call in the
  same batch, so resuming never re-executes a BigQuery query.
- ``user_id`` comes from the session config and is injected by the dispatcher;
  it is not a model-visible parameter.
- ``finalize`` runs the layer-3 output scan on the final answer.
"""

import json
import logging
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from langchain_core.runnables import RunnableConfig

from . import config as cfg
from . import pii, prompts, sqlguard
from .bq import BigQueryRunner, QueryFailed
from .prefs import PreferenceStore
from .reports import ReportLibrary
from .trios import GoldenBucket, format_trios

logger = logging.getLogger(__name__)

EXHAUSTED_MESSAGE = (
    "QUERY BUDGET EXHAUSTED: you have used all "
    f"{cfg.MAX_SQL_ATTEMPTS} SQL attempts for this request. Do not call run_sql "
    "again. Tell the user plainly what you tried, what failed, and suggest how they "
    "could rephrase the question."
)


class AgentState(MessagesState):
    sql_failures: int
    pii_strict: bool
    trio_context: str


# --- Tool schemas (bodies never run; execution is dispatched in the tool node) ---

@tool
def get_schema() -> str:
    """Get the schema (tables, columns, types) of the analytics dataset."""
    raise NotImplementedError


@tool
def run_sql(sql: str) -> str:
    """Run a single BigQuery SELECT query against the analytics dataset and get
    the resulting rows. Only SELECT is allowed; results are capped and
    PII-masked. On error, fix the query based on the message and try again."""
    raise NotImplementedError


@tool
def save_report(title: str, body: str) -> str:
    """Save a finished analyst report to the user's private report library."""
    raise NotImplementedError


@tool
def list_reports() -> str:
    """List the user's saved reports (id, title, creation date)."""
    raise NotImplementedError


@tool
def get_report(report_id: int) -> str:
    """Fetch the full text of one saved report by id."""
    raise NotImplementedError


@tool
def delete_reports(
    mentioning: str = "", created_on: str = "", report_ids: Optional[list[int]] = None
) -> str:
    """Delete saved reports matching the given criteria (text they mention,
    ISO creation date YYYY-MM-DD, and/or explicit ids). The user is always
    shown the matching reports and asked to confirm before anything is
    deleted."""
    raise NotImplementedError


@tool
def update_preference(key: str, value: str) -> str:
    """Store a durable presentation preference for this user, e.g.
    key='format', value='tables'. Pass an empty value to clear a preference."""
    raise NotImplementedError


TOOLS = [get_schema, run_sql, save_report, list_reports, get_report, delete_reports,
         update_preference]


class Agent:
    """Wires components into a compiled LangGraph. One instance per process."""

    def __init__(self, checkpointer=None) -> None:
        self.runner = BigQueryRunner()
        self.bucket = GoldenBucket()
        self.library = ReportLibrary()
        self.prefs = PreferenceStore()
        self._schema_cache: Optional[str] = None
        self.llm = self._build_llm()
        self.graph = self._build_graph(checkpointer)

    # --- model with retry + provider fallback (design §4.5, llm.py) -----------

    def _build_llm(self):
        from .llm import build_chat_model

        return build_chat_model(TOOLS)

    # --- nodes -----------------------------------------------------------------

    def _agent_node(self, state: AgentState, config: RunnableConfig) -> dict:
        updates: dict[str, Any] = {}
        last = state["messages"][-1]
        if isinstance(last, HumanMessage):
            # New user turn: reset the retry budget and refresh retrieval.
            updates["sql_failures"] = 0
            updates["pii_strict"] = False
            trios = self.bucket.retrieve(str(last.content))
            updates["trio_context"] = format_trios(trios)

        user_id = config["configurable"]["user_id"]
        system = prompts.build_system_prompt(self.prefs.get(user_id))
        trio_context = updates.get("trio_context", state.get("trio_context", ""))
        if trio_context:
            system += "\n" + trio_context
        response = self.llm.invoke([SystemMessage(system), *state["messages"]])
        updates["messages"] = [response]
        return updates

    def _tools_node(self, state: AgentState, config: RunnableConfig) -> dict:
        user_id = config["configurable"]["user_id"]
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)  # routed here only on tool_calls
        failures = state.get("sql_failures", 0)
        pii_strict = state.get("pii_strict", False)
        results: list[ToolMessage] = []

        # Interrupt-safety: delete_reports first, so a resume replays no queries.
        calls = sorted(
            last.tool_calls, key=lambda c: 0 if c["name"] == "delete_reports" else 1
        )
        for call in calls:
            try:
                if call["name"] == "run_sql":
                    content, failures, touched = self._exec_run_sql(
                        call["args"].get("sql", ""), failures
                    )
                    pii_strict = pii_strict or touched
                else:
                    content = self._exec_other(call["name"], call["args"], user_id)
            except (QueryFailed, sqlguard.GuardViolation) as e:  # belt and braces
                content = str(e)
            results.append(
                ToolMessage(content=content, tool_call_id=call["id"], name=call["name"])
            )
        return {"messages": results, "sql_failures": failures, "pii_strict": pii_strict}

    def _finalize_node(self, state: AgentState) -> dict:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not isinstance(last.content, str):
            return {}
        # Pattern-only (emails/phones): layer 2 guarantees the model never saw a
        # real customer name, so person-"names" in the prose are business terms
        # or hallucinations — NER-masking here would only degrade reports.
        cleaned, hits = pii.scan_output(last.content)
        if not hits:
            return {}
        return {"messages": [AIMessage(content=cleaned, id=last.id)]}

    # --- tool implementations ----------------------------------------------------

    def _exec_run_sql(self, sql: str, failures: int) -> tuple[str, int, bool]:
        """validate → dry-run → execute → mask. Returns (content, failures, touched_pii)."""
        if failures >= cfg.MAX_SQL_ATTEMPTS:
            return EXHAUSTED_MESSAGE, failures, False
        try:
            validated = sqlguard.validate(sql)
        except sqlguard.GuardViolation as e:
            return f"Query rejected by policy: {e}", failures + 1, False
        try:
            result = self.runner.execute(validated.sql)
        except QueryFailed as e:
            return str(e), failures + 1, validated.touches_pii_table
        if result.empty:
            return (
                "Query ran but returned 0 rows. Either an assumption is wrong (check "
                "filter values, join keys, date ranges — a small diagnostic query can "
                "verify) or there is genuinely no matching data. Verify before "
                "concluding.",
                failures + 1,
                validated.touches_pii_table,
            )
        masked_rows, masked_count = pii.mask_result_rows(
            result.rows,
            validated.touches_pii_table,
            ner_exempt=validated.ner_exempt_columns,
        )
        payload = {
            "total_rows": result.total_rows,
            "returned_rows": len(masked_rows),
            "truncated": result.truncated,
            "masked_pii_values": masked_count,
            "rows": masked_rows,
        }
        return json.dumps(payload, default=str), 0, validated.touches_pii_table

    def _exec_other(self, name: str, args: dict, user_id: str) -> str:
        if name == "get_schema":
            if self._schema_cache is None:
                self._schema_cache = self.runner.schema_context()
            return self._schema_cache

        if name == "save_report":
            report_id = self.library.save(
                user_id, args.get("title", "Untitled"), args.get("body", "")
            )
            return f"Saved as report #{report_id}."

        if name == "list_reports":
            reports = self.library.list_reports(user_id)
            if not reports:
                return "No saved reports."
            return "\n".join(
                f"#{r.id} — {r.title} (created {r.created_at[:10]})" for r in reports
            )

        if name == "get_report":
            report = self.library.get(user_id, int(args["report_id"]))
            return (
                f"#{report.id} — {report.title} ({report.created_at[:10]})\n\n{report.body}"
                if report
                else "No such report in your library."
            )

        if name == "delete_reports":
            return self._exec_delete(args, user_id)

        if name == "update_preference":
            prefs = self.prefs.set(user_id, args.get("key", ""), args.get("value", ""))
            return f"Preferences updated: {json.dumps(prefs)}"

        return f"Unknown tool: {name}"

    def _exec_delete(self, args: dict, user_id: str) -> str:
        matches = self.library.find(
            user_id,
            mentioning=args.get("mentioning") or None,
            created_on=args.get("created_on") or None,
            report_ids=args.get("report_ids") or None,
        )
        if not matches:
            return "No reports match those criteria (only your own reports are searchable)."
        # Pause the graph; the CLI shows this payload and asks for confirmation.
        decision = interrupt(
            {
                "action": "delete_reports",
                "matches": [
                    {"id": r.id, "title": r.title, "created": r.created_at[:10]}
                    for r in matches
                ],
            }
        )
        if str(decision).strip().lower() in {"y", "yes", "confirm", "delete"}:
            n = self.library.soft_delete(user_id, [r.id for r in matches])
            return f"Deleted {n} report(s). They are recoverable for 30 days."
        return "Deletion cancelled by the user. Nothing was deleted."

    # --- graph -------------------------------------------------------------------

    def _build_graph(self, checkpointer):
        def route(state: AgentState) -> str:
            last = state["messages"][-1]
            return "tools" if getattr(last, "tool_calls", None) else "finalize"

        builder = StateGraph(AgentState)
        builder.add_node("agent", self._agent_node)
        builder.add_node("tools", self._tools_node)
        builder.add_node("finalize", self._finalize_node)
        builder.add_edge(START, "agent")
        builder.add_conditional_edges("agent", route, {"tools": "tools", "finalize": "finalize"})
        builder.add_edge("tools", "agent")
        builder.add_edge("finalize", END)
        return builder.compile(checkpointer=checkpointer)

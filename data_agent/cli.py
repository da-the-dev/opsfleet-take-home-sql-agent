"""CLI chat interface.

The chat surface for the prototype: streams agent progress, renders reports as
markdown, and hosts the human-in-the-loop confirmation for destructive report
operations (the LangGraph interrupt surfaces here as a y/N prompt). The UI
never crashes on agent errors — failures render as messages and the loop
continues (design §4.5).
"""

import argparse
import logging
import os
import sqlite3
import sys
import uuid

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from . import config as cfg
from . import pii
from .graph import Agent

logger = logging.getLogger(__name__)


def _langfuse_callbacks() -> list:
    """Langfuse tracing, enabled only when keys are configured (design §4.7)."""
    if not os.getenv("LANGFUSE_PUBLIC_KEY"):
        return []
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]
    except Exception:  # noqa: BLE001 - observability never blocks the user
        logger.exception("Langfuse configured but failed to initialize; continuing untraced")
        return []


def _confirm_deletion(console: Console, payload: dict) -> str:
    """Render the delete preview and collect the user's decision."""
    table = Table(title="These reports will be deleted", title_style="bold red")
    table.add_column("id", justify="right")
    table.add_column("title")
    table.add_column("created")
    for m in payload.get("matches", []):
        table.add_row(str(m["id"]), m["title"], m["created"])
    console.print(table)
    return console.input("[bold red]Delete these reports? \\[y/N][/] ").strip() or "n"


def _turn(agent: Agent, console: Console, run_config: dict, user_input) -> None:
    """One user turn: stream the graph, surface interrupts, print the answer."""
    payload = {"messages": [HumanMessage(user_input)]}
    while True:  # loops only when an interrupt needs resuming
        final: AIMessage | None = None
        interrupted = None
        with console.status("[dim]analyzing…[/]", spinner="dots"):
            for update in agent.graph.stream(payload, run_config, stream_mode="updates"):
                if "__interrupt__" in update:
                    interrupted = update["__interrupt__"][0]
                    continue
                for node_output in update.values():
                    for message in (node_output or {}).get("messages", []):
                        if isinstance(message, AIMessage) and message.tool_calls:
                            for call in message.tool_calls:
                                detail = ""
                                if call["name"] == "run_sql":
                                    sql = str(call["args"].get("sql", ""))
                                    detail = ": " + " ".join(sql.split())[:100] + "…"
                                console.print(f"[dim]⚙ {call['name']}{detail}[/]")
                        elif isinstance(message, AIMessage):
                            final = message
        if interrupted is not None:
            payload = Command(resume=_confirm_deletion(console, interrupted.value))
            continue
        if final is not None and isinstance(final.content, str) and final.content.strip():
            console.print(Panel(Markdown(final.content), border_style="green"))
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Data analysis chat agent (thelook_ecommerce)")
    parser.add_argument("--user", default="manager_a", help="acts as this user id (session auth stand-in)")
    parser.add_argument("--thread", default=None, help="conversation id to resume; default starts fresh")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    console = Console()

    try:
        from .llm import build_chat_model  # fail fast on provider misconfiguration

        build_chat_model([])
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]{e}[/]")
        sys.exit(1)

    thread = args.thread or uuid.uuid4().hex[:8]
    run_config = {
        "configurable": {"thread_id": f"{args.user}:{thread}", "user_id": args.user},
        "callbacks": _langfuse_callbacks(),
        "recursion_limit": 50,
    }

    with console.status("[dim]starting up (BigQuery, NER, embeddings)…[/]", spinner="dots"):
        checkpointer = SqliteSaver(sqlite3.connect(cfg.CHECKPOINT_DB, check_same_thread=False))
        agent = Agent(checkpointer)
        pii.warm_up()

    console.print(
        Panel(
            f"Signed in as [bold]{args.user}[/] · thread [bold]{thread}[/]\n"
            "Ask about sales, products, customers — or manage your saved reports.\n"
            "[dim]Ctrl-D or 'exit' to quit.[/]",
            title="Data Analysis Assistant",
            border_style="cyan",
        )
    )

    while True:
        try:
            user_input = console.input(f"[bold cyan]{args.user}>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/]")
            break
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "q"}:
            console.print("[dim]bye[/]")
            break
        try:
            _turn(agent, console, run_config, user_input)
        except Exception as e:  # noqa: BLE001 - the chat surface never crashes
            logger.exception("turn failed")
            console.print(
                Panel(
                    f"Something went wrong on my side: {e}\n"
                    "Your session is intact — try again or rephrase.",
                    border_style="red",
                )
            )


if __name__ == "__main__":
    main()

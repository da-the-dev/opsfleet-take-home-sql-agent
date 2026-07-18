"""CLI chat interface.

The chat surface for the prototype: streams agent progress, renders reports as
markdown, and hosts the human-in-the-loop confirmation for destructive report
operations (the LangGraph interrupt surfaces here as a y/N prompt). The UI
never crashes on agent errors — failures render as messages and the loop
continues (design §4.5).

Slash commands (/help, /reports, /resume, …) are handled locally in this
layer and never reach the model: they read the same report library and
checkpoint store the agent uses, so they are instant, free, and deterministic.
Plain-language equivalents ("show my saved reports") still work via the
agent's tools.
"""

import argparse
import logging
import os
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from . import config as cfg
from . import pii
from .graph import Agent
from .reports import ReportLibrary

logger = logging.getLogger(__name__)


HELP = """\
**What I can do**
- Answer business questions over the company dataset (orders, products, \
customers) — trends, comparisons, "why" questions.
- Save analyses as reports and manage your private report library \
(deletions always ask for confirmation first).
- Remember presentation preferences ("always show me tables").

**Slash commands** *(handled locally — instant, never sent to the model)*

| command | what it does |
|---|---|
| `/help` | this message |
| `/reports` | list your saved reports |
| `/report <id>` | show a saved report |
| `/threads` | list your past conversations |
| `/resume <id>` | switch to a past conversation (full context restored) |
| `/new` | start a fresh conversation |
| `exit` | quit (also Ctrl-D) |

Plain language works for all of this too — "show my saved reports" or \
"delete the Texas report" go through the agent. The commands are just the \
fast path.\
"""


@dataclass
class Session:
    """Mutable per-session state the slash commands read and update."""

    console: Console
    library: ReportLibrary
    checkpoint_conn: sqlite3.Connection
    user: str
    thread: str
    callbacks: list = field(default_factory=list)

    @property
    def run_config(self) -> dict:
        return {
            "configurable": {"thread_id": f"{self.user}:{self.thread}", "user_id": self.user},
            "callbacks": self.callbacks,
            "recursion_limit": 50,
        }

    def known_threads(self) -> list[str]:
        """Past conversation ids for this user, from the checkpoint store."""
        try:
            rows = self.checkpoint_conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ?",
                (f"{self.user}:%",),
            ).fetchall()
        except sqlite3.OperationalError:  # fresh DB, no checkpoints table yet
            return []
        return sorted(r[0].split(":", 1)[1] for r in rows)


def handle_command(session: Session, line: str) -> bool:
    """Dispatch a local slash command. Returns False if `line` is not one."""
    if not line.startswith("/"):
        return False
    console = session.console
    cmd, _, arg = line[1:].partition(" ")
    cmd, arg = cmd.lower(), arg.strip()

    if cmd == "help":
        console.print(Panel(Markdown(HELP), title="Help", border_style="cyan"))
    elif cmd == "reports":
        reports = session.library.list_reports(session.user)
        if not reports:
            console.print("[dim]No saved reports yet — ask me to save an analysis.[/]")
        else:
            table = Table(title=f"Saved reports · {session.user}")
            table.add_column("id", justify="right")
            table.add_column("title")
            table.add_column("created")
            for r in reports:
                table.add_row(str(r.id), r.title, r.created_at[:16])
            table.caption = "Show one with /report <id>"
            console.print(table)
    elif cmd == "report":
        if not arg.isdigit():
            console.print("[dim]Usage: /report <id> — see ids with /reports[/]")
        elif report := session.library.get(session.user, int(arg)):
            console.print(Panel(Markdown(report.body), title=report.title, border_style="green"))
        else:
            console.print(f"[dim]No report with id {arg} — see /reports[/]")
    elif cmd == "threads":
        threads = session.known_threads()
        if not threads:
            console.print("[dim]No past conversations yet.[/]")
        else:
            for t in threads:
                marker = " [cyan](current)[/]" if t == session.thread else ""
                console.print(f"  {t}{marker}")
            console.print("[dim]Switch with /resume <id>[/]")
    elif cmd == "resume":
        if not arg:
            console.print("[dim]Usage: /resume <id> — see ids with /threads[/]")
        else:
            fresh = arg not in session.known_threads()
            session.thread = arg
            note = " (new thread — no history yet)" if fresh else " — context restored"
            console.print(f"[cyan]Now on thread [bold]{arg}[/]{note}[/]")
    elif cmd == "new":
        session.thread = uuid.uuid4().hex[:8]
        console.print(f"[cyan]Started fresh thread [bold]{session.thread}[/][/]")
    else:
        console.print(f"[dim]Unknown command /{cmd} — try /help[/]")
    return True


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
    payload: Command | dict[str, list[BaseMessage]]= {"messages": [HumanMessage(user_input)]}
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
    # Presidio warns about every non-English recognizer it skips — noise that
    # would bury the welcome screen.
    logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)
    console = Console()

    try:
        from .llm import build_chat_model  # fail fast on provider misconfiguration

        build_chat_model([])
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]{e}[/]")
        sys.exit(1)

    with console.status("[dim]starting up (BigQuery, NER, embeddings)…[/]", spinner="dots"):
        checkpoint_conn = sqlite3.connect(cfg.CHECKPOINT_DB, check_same_thread=False)
        agent = Agent(SqliteSaver(checkpoint_conn))
        pii.warm_up()

    session = Session(
        console=console,
        library=agent.library,
        checkpoint_conn=checkpoint_conn,
        user=args.user,
        thread=args.thread or uuid.uuid4().hex[:8],
        callbacks=_langfuse_callbacks(),
    )

    console.print(
        Panel(
            f"Signed in as [bold]{args.user}[/] · thread [bold]{session.thread}[/]\n\n"
            "I answer business questions about sales, products, and customers,\n"
            "and keep your private library of saved reports. Try:\n"
            '  [italic]"How did revenue trend over the last 6 months?"[/]\n'
            '  [italic]"Compare Texas and California customers — why the gap?"[/]\n'
            '  [italic]"Save that as a report"  ·  "Show my saved reports"[/]\n\n'
            "[dim]/help · /reports · /report <id> · /threads · /resume <id> · /new"
            " · 'exit' or Ctrl-D to quit[/]",
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
            if handle_command(session, user_input):
                continue
            _turn(agent, console, session.run_config, user_input)
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

"""Offline tests of the local slash-command layer.

Commands never touch the model or network — they read the same report
library and checkpoint store the agent uses, so a real Console (recording
mode) plus a temp library exercises them fully.
"""

import sqlite3

from rich.console import Console

from data_agent.cli import Session, handle_command
from data_agent.reports import ReportLibrary


def make_session(tmp_path, thread="t1"):
    console = Console(record=True, width=100)
    session = Session(
        console=console,
        library=ReportLibrary(tmp_path / "r.db"),
        checkpoint_conn=sqlite3.connect(":memory:"),
        user="manager_a",
        thread=thread,
    )
    return session, console


def test_non_command_passes_through(tmp_path):
    session, _ = make_session(tmp_path)
    assert handle_command(session, "compare texas to california") is False


def test_unknown_command_not_sent_to_model(tmp_path):
    session, console = make_session(tmp_path)
    assert handle_command(session, "/repotrs") is True  # typo stays local
    assert "/help" in console.export_text()


def test_reports_lists_only_own_reports(tmp_path):
    session, console = make_session(tmp_path)
    session.library.save("manager_a", "Texas vs California", "body a")
    session.library.save("manager_b", "Secret plan", "body b")
    assert handle_command(session, "/reports") is True
    text = console.export_text()
    assert "Texas vs California" in text
    assert "Secret plan" not in text


def test_report_shows_body_and_handles_missing(tmp_path):
    session, console = make_session(tmp_path)
    rid = session.library.save("manager_a", "Q2 review", "Revenue grew 12%.")
    assert handle_command(session, f"/report {rid}") is True
    assert "Revenue grew 12%" in console.export_text()

    assert handle_command(session, "/report 999") is True
    assert "No report with id 999" in console.export_text()

    assert handle_command(session, "/report abc") is True
    assert "Usage" in console.export_text()


def test_new_and_resume_switch_threads(tmp_path):
    session, console = make_session(tmp_path, thread="orig")
    assert handle_command(session, "/new") is True
    assert session.thread != "orig"
    assert session.run_config["configurable"]["thread_id"] == f"manager_a:{session.thread}"

    assert handle_command(session, "/resume orig") is True
    assert session.thread == "orig"


def test_threads_empty_on_fresh_db(tmp_path):
    session, console = make_session(tmp_path)
    assert handle_command(session, "/threads") is True
    assert "No past conversations" in console.export_text()


def test_threads_lists_own_threads_from_checkpoints(tmp_path):
    session, console = make_session(tmp_path, thread="aaa")
    conn = session.checkpoint_conn
    conn.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)")
    conn.executemany(
        "INSERT INTO checkpoints VALUES (?, ?)",
        [("manager_a:aaa", "1"), ("manager_a:aaa", "2"),
         ("manager_a:bbb", "1"), ("manager_b:zzz", "1")],
    )
    assert session.known_threads() == ["aaa", "bbb"]  # manager_b invisible
    assert handle_command(session, "/threads") is True
    text = console.export_text()
    assert "aaa" in text and "(current)" in text
    assert "zzz" not in text

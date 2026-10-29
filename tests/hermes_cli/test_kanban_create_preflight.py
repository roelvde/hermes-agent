"""Create-time spawn preflight (F-05): ``kanban create`` (CLI) and ``kanban_create`` (tool) refuse a
task whose pinned skill the assignee profile cannot load, instead of crash-looping at spawn.

Integration level: a real isolated HERMES_HOME with real profile dirs and SKILL.md files, the real
skill resolver (``tools.skills_tool._skill_catalog``), the real parser/handler and a real board DB.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools as kt


def _skill(root: Path, rel: str, name: str | None = None) -> None:
    d = root / "skills" / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name or rel.rsplit('/', 1)[-1]}\ndescription: test skill {rel}\n---\nBody\n",
        encoding="utf-8",
    )


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for prof in ("alpha", "beta"):
        pdir = home / "profiles" / prof
        pdir.mkdir(parents=True)
        (pdir / "config.yaml").write_text("{}\n", encoding="utf-8")
    _skill(home / "profiles" / "alpha", "tools/alpha-skill")
    _skill(home / "profiles" / "alpha", "tools/renamed-dir", name="declared-name")
    _skill(home / "profiles" / "beta", "tools/beta-only")
    _skill(home, "tools/default-skill")
    kb.init_db()
    return home


def _tasks():
    with kbc.connect_closing() as conn:
        return kb.list_tasks(conn)


def _tool_create(**args):
    return json.loads(kt._handle_create({"title": "t", **args}))


def _cli_create(argv, capsys):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    ns = root.parse_args(["kanban", "create", "t", *argv])
    rc = kc._cmd_create(ns)
    return rc, capsys.readouterr()


# --- tool: kanban_create ---------------------------------------------------------------------

def test_tool_create_refuses_skill_missing_on_assignee(board):
    out = _tool_create(assignee="alpha", skills=["no-such-skill"])
    assert "error" in out and "task_id" not in out, out
    assert "alpha" in out["error"] and "no-such-skill" in out["error"]
    assert "preflight:unknown_skill" in out["error"]
    assert _tasks() == []


def test_tool_create_refusal_hints_where_skill_exists(board):
    out = _tool_create(assignee="alpha", skills=["beta-only"])
    assert "error" in out, out
    assert "beta-only" in out["error"] and "beta" in out["error"]
    assert "exists on" in out["error"]
    assert _tasks() == []


def test_tool_create_refuses_when_one_of_several_skills_is_missing(board):
    out = _tool_create(assignee="alpha", skills=["alpha-skill", "ghost"])
    assert "error" in out and "ghost" in out["error"]
    assert _tasks() == []


def test_tool_create_known_skill_is_stored_unchanged(board):
    out = _tool_create(assignee="alpha", skills=["alpha-skill"])
    assert out.get("ok") is True, out
    (task,) = _tasks()
    assert list(task.skills) == ["alpha-skill"]


def test_tool_create_normalizes_category_prefix(board):
    out = _tool_create(assignee="alpha", skills=["wrong-category:alpha-skill"])
    assert out.get("ok") is True, out
    (task,) = _tasks()
    assert list(task.skills) == ["alpha-skill"]


def test_tool_create_without_skills_is_unchanged_even_for_unknown_assignee(board):
    out = _tool_create(assignee="nobody-here")
    assert out.get("ok") is True, out
    assert len(_tasks()) == 1


def test_tool_create_unknown_assignee_with_skills_is_refused(board):
    out = _tool_create(assignee="nobody-here", skills=["alpha-skill"])
    assert "error" in out and "nobody-here" in out["error"]
    assert "preflight:unknown_assignee" in out["error"]
    assert _tasks() == []


def test_tool_create_fails_open_when_catalog_cannot_be_built(board, monkeypatch):
    def boom(profiles):
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr("hermes_cli.kanban_preflight_io.profile_catalogs", boom)
    out = _tool_create(assignee="alpha", skills=["no-such-skill"])
    assert out.get("ok") is True, out
    assert len(_tasks()) == 1


# --- CLI: hermes kanban create ---------------------------------------------------------------

def test_cli_create_refuses_skill_missing_on_assignee(board, capsys):
    rc, cap = _cli_create(["--assignee", "alpha", "--skill", "no-such-skill"], capsys)
    assert rc != 0
    assert "alpha" in cap.err and "no-such-skill" in cap.err
    assert "preflight:unknown_skill" in cap.err
    assert _tasks() == []


def test_cli_create_known_skill_succeeds_and_stores_it(board, capsys):
    rc, cap = _cli_create(["--assignee", "alpha", "--skill", "alpha-skill"], capsys)
    assert rc == 0, cap.err
    (task,) = _tasks()
    assert list(task.skills) == ["alpha-skill"]


def test_cli_create_normalizes_category_prefix(board, capsys):
    rc, cap = _cli_create(["--assignee", "alpha", "--skill", "x/alpha-skill"], capsys)
    assert rc == 0, cap.err
    (task,) = _tasks()
    assert list(task.skills) == ["alpha-skill"]


def test_cli_create_without_skills_is_unchanged(board, capsys):
    rc, cap = _cli_create(["--assignee", "nobody-here"], capsys)
    assert rc == 0, cap.err
    assert len(_tasks()) == 1


# --- catalog builder --------------------------------------------------------------------------

def test_profile_skill_names_follow_the_profile_home(board):
    from hermes_cli.kanban_preflight_io import profile_catalogs

    cat = profile_catalogs(["alpha", "beta", "default"])
    assert "tools/alpha-skill" in cat["alpha"] and "declared-name" in cat["alpha"]
    assert "tools/beta-only" not in cat["alpha"]
    assert "tools/beta-only" in cat["beta"]
    assert "tools/default-skill" in cat["default"] and "tools/alpha-skill" not in cat["default"]


def test_profile_skill_names_leave_out_disabled_skills(board):
    from hermes_cli.kanban_preflight_io import profile_catalogs

    (board / "profiles" / "alpha" / "config.yaml").write_text(
        "skills:\n  disabled:\n    - alpha-skill\n", encoding="utf-8")
    assert "tools/alpha-skill" not in profile_catalogs(["alpha"])["alpha"]
    out = _tool_create(assignee="alpha", skills=["alpha-skill"])
    assert "error" in out and "alpha-skill" in out["error"]

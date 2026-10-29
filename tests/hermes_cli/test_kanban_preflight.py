"""Tests for hermes_cli.kanban_preflight.validate_task_spawnable (pure spawnability preflight)."""

import builtins
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli.kanban_preflight import (
    REASON_AMBIGUOUS_SKILL,
    REASON_INVALID_SKILLS,
    REASON_NO_ASSIGNEE,
    REASON_UNKNOWN_ASSIGNEE,
    REASON_UNKNOWN_SKILL,
    normalize_assignee,
    normalize_skill_name,
    validate_task_spawnable,
)

PROFILES = {
    "default": ["research/igo-daily-scan", "research/igo-competitor-monitor", "devops/kanban-worker",
                "autonomous-ai-agents/hermes-agent", "productivity/reviewer-handoff"],
    "builder": ["devops/kanban-worker", "autonomous-ai-agents/hermes-agent", "productivity/reviewer-handoff",
                "superpowers:writing-plans"],
    "reviewer": ["autonomous-ai-agents/hermes-agent", "devops/sdlc-review"],
    "bert": [],
}


def test_known_skills_pass_unchanged():
    task = {"assignee": "builder", "skills": ["kanban-worker", "hermes-agent", "superpowers:writing-plans"]}
    res = validate_task_spawnable(task, PROFILES)
    assert res.spawnable is True
    assert res.reason is None
    assert list(res.skills) == ["kanban-worker", "hermes-agent", "superpowers:writing-plans"]
    assert dict(res.renamed) == {}
    assert res.unknown_skills == () and res.problems == ()


def test_no_skills_is_spawnable_with_empty_list():
    for skills in (None, [], "", "[]"):
        res = validate_task_spawnable({"assignee": "builder", "skills": skills}, PROFILES)
        assert res.spawnable is True, skills
        assert res.skills == ()


def test_category_prefix_is_normalized_to_bare_name():
    task = {"assignee": "builder",
            "skills": ["software-development:kanban-worker", "devops/kanban-worker", " devops:kanban-worker ",
                       "autonomous-ai-agents/hermes-agent"]}
    res = validate_task_spawnable(task, PROFILES)
    assert res.spawnable is True
    assert list(res.skills) == ["kanban-worker", "hermes-agent"]  # bare and de-duplicated
    assert res.renamed["software-development:kanban-worker"] == "kanban-worker"
    assert res.renamed["autonomous-ai-agents/hermes-agent"] == "hermes-agent"
    assert "normalized" in res.message


@pytest.mark.parametrize("raw,bare", [
    ("kanban-worker", "kanban-worker"),
    ("software-development:kanban-worker", "kanban-worker"),
    ("devops/kanban-worker", "kanban-worker"),
    ("a/b/c-skill", "c-skill"),
    ("  spaced  ", "spaced"),
    ("", ""),
])
def test_normalize_skill_name(raw, bare):
    assert normalize_skill_name(raw) == bare


def test_unknown_skill_is_not_spawnable_with_reason():
    task = {"assignee": "builder", "skills": ["igo-daily-scan", "igo-competitor-monitor"]}
    res = validate_task_spawnable(task, PROFILES)
    assert res.spawnable is False
    assert res.reason == REASON_UNKNOWN_SKILL
    assert list(res.unknown_skills) == ["igo-daily-scan", "igo-competitor-monitor"]
    assert "Unknown skill(s) for profile 'builder': igo-daily-scan, igo-competitor-monitor" in res.message
    assert res.available_on["igo-daily-scan"] == ("default",)
    assert "'igo-daily-scan' exists on: default" in res.message


def test_partially_missing_skill_set_is_refused():
    # The worker would silently run without the missing skill; a pinned skill is a contract.
    res = validate_task_spawnable({"assignee": "reviewer", "skills": ["hermes-agent", "reviewer-handoff"]}, PROFILES)
    assert res.spawnable is False
    assert res.reason == REASON_UNKNOWN_SKILL
    assert list(res.unknown_skills) == ["reviewer-handoff"]
    assert list(res.skills) == ["hermes-agent"]


def test_wrong_category_and_missing_skill_still_refused():
    res = validate_task_spawnable({"assignee": "bert", "skills": ["software-development:kanban-worker"]}, PROFILES)
    assert res.spawnable is False
    assert res.reason == REASON_UNKNOWN_SKILL
    assert list(res.unknown_skills) == ["software-development:kanban-worker"]
    assert set(res.available_on["software-development:kanban-worker"]) == {"builder", "default"}


def test_unknown_assignee_is_not_spawnable_with_reason():
    for assignee in ("main", "worksafe"):
        res = validate_task_spawnable({"assignee": assignee, "skills": ["kanban-worker"]}, PROFILES)
        assert res.spawnable is False
        assert res.reason == REASON_UNKNOWN_ASSIGNEE
        assert f"Unknown assignee profile '{assignee}'" in res.message


def test_missing_assignee_is_not_spawnable():
    for assignee in (None, "", "   ", 0):
        res = validate_task_spawnable({"assignee": assignee, "skills": []}, PROFILES)
        assert res.spawnable is False
        assert res.reason == REASON_NO_ASSIGNEE


def test_assignee_is_normalized_like_profiles_module():
    from hermes_cli.profiles import normalize_profile_name

    for raw in ("Builder", " builder ", "DEFAULT", "Default", "reviewer"):
        assert normalize_assignee(raw) == normalize_profile_name(raw)
    res = validate_task_spawnable({"assignee": "Builder", "skills": ["kanban-worker"]}, PROFILES)
    assert res.spawnable is True and res.assignee == "builder"
    assert validate_task_spawnable({"assignee": "DEFAULT", "skills": None}, PROFILES).spawnable is True


def test_profile_without_skills_exists():
    res = validate_task_spawnable({"assignee": "bert", "skills": None}, PROFILES)
    assert res.spawnable is True
    assert validate_task_spawnable({"assignee": "bert", "skills": ["kanban-worker"]}, PROFILES).spawnable is False


def test_db_row_with_json_skills_string():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("create table tasks (id text, assignee text, skills text)")
    con.execute("insert into tasks values ('t_1', 'reviewer', ?)", (json.dumps(["hermes-agent", "reviewer-handoff"]),))
    con.execute("insert into tasks values ('t_2', 'builder', ?)", (json.dumps(["hermes-agent"]),))
    rows = {r["id"]: r for r in con.execute("select * from tasks")}
    assert validate_task_spawnable(rows["t_1"], PROFILES).reason == REASON_UNKNOWN_SKILL
    assert list(validate_task_spawnable(rows["t_2"], PROFILES).skills) == ["hermes-agent"]


def test_task_object_with_attributes():
    task = SimpleNamespace(id="t_x", assignee="builder", skills=["devops/kanban-worker"])
    res = validate_task_spawnable(task, PROFILES)
    assert res.spawnable is True and list(res.skills) == ["kanban-worker"]


@pytest.mark.parametrize("skills", ["[not json", {"a": 1}, ["ok", 3], 42])
def test_malformed_skills_field_is_refused(skills):
    res = validate_task_spawnable({"assignee": "builder", "skills": skills}, PROFILES)
    assert res.spawnable is False
    assert res.reason == REASON_INVALID_SKILLS


def test_single_string_skill_and_blank_entries():
    assert list(validate_task_spawnable({"assignee": "builder", "skills": "kanban-worker"}, PROFILES).skills) == [
        "kanban-worker"]
    res = validate_task_spawnable({"assignee": "builder", "skills": ["", "  ", "hermes-agent"]}, PROFILES)
    assert res.spawnable is True and list(res.skills) == ["hermes-agent"]


def test_ambiguous_bare_name_is_refused_but_exact_qualified_name_picks_one():
    profiles = {"builder": ["devops/claude-code", "autonomous-ai-agents/claude-code"]}
    res = validate_task_spawnable({"assignee": "builder", "skills": ["claude-code"]}, profiles)
    assert res.spawnable is False
    assert res.reason == REASON_AMBIGUOUS_SKILL
    assert list(res.ambiguous_skills) == ["claude-code"]
    ok = validate_task_spawnable({"assignee": "builder", "skills": ["devops:claude-code"]}, profiles)
    assert ok.spawnable is True and list(ok.skills) == ["devops/claude-code"]
    wrong = validate_task_spawnable({"assignee": "builder", "skills": ["research/claude-code"]}, profiles)
    assert wrong.reason == REASON_AMBIGUOUS_SKILL


def test_unknown_reason_takes_precedence_over_ambiguous():
    profiles = {"builder": ["a/x", "b/x"]}
    res = validate_task_spawnable({"assignee": "builder", "skills": ["x", "nope"]}, profiles)
    assert res.reason == REASON_UNKNOWN_SKILL
    assert len(res.problems) == 2


def test_plugin_skill_keeps_qualified_name():
    res = validate_task_spawnable({"assignee": "builder", "skills": ["writing-plans"]}, PROFILES)
    assert res.spawnable is True and list(res.skills) == ["superpowers:writing-plans"]


def test_function_is_pure(monkeypatch):
    task = {"assignee": "builder", "skills": ["software-development:kanban-worker", "igo-daily-scan"]}
    profiles = copy.deepcopy(PROFILES)
    task_before, profiles_before = copy.deepcopy(task), copy.deepcopy(profiles)

    def no_io(*_a, **_k):
        raise AssertionError("validate_task_spawnable must not do I/O")

    monkeypatch.setattr(builtins, "open", no_io)
    first = validate_task_spawnable(task, profiles)
    second = validate_task_spawnable(task, profiles)
    assert first == second
    assert task == task_before and profiles == profiles_before


@pytest.mark.parametrize("assignee,skills,catalog,reason", [
    # The four unknown-skill crash-storm shapes (kanban-gaps §4.1), with the catalog the worker saw.
    ("builder", '["igo-daily-scan", "igo-competitor-monitor"]', {"builder": ["devops/kanban-worker"]},
     REASON_UNKNOWN_SKILL),
    ("worksafe", '["igo-daily-scan", "igo-competitor-monitor"]', {"worksafe": ["devops/kanban-worker"]},
     REASON_UNKNOWN_SKILL),
    ("reviewer", '["hermes-agent", "reviewer-handoff"]', {"reviewer": ["autonomous-ai-agents/hermes-agent"]},
     REASON_UNKNOWN_SKILL),
    ("main", '["software-development:kanban-worker"]', {"main": []}, REASON_UNKNOWN_SKILL),
    ("main", None, {"builder": []}, REASON_UNKNOWN_ASSIGNEE),
])
def test_crash_storm_shapes_are_caught(assignee, skills, catalog, reason):
    res = validate_task_spawnable({"assignee": assignee, "skills": skills}, catalog)
    assert res.spawnable is False
    assert res.reason == reason
    assert res.message

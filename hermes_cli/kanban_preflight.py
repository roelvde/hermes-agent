"""Spawnability preflight for kanban tasks: a pure check run before create and before spawn.

97% of all crashed kanban runs on this install were ``Unknown skill(s): ...`` at worker start
(a skill pinned on the card that the assignee profile does not have, or a category-qualified
name such as ``software-development:kanban-worker`` whose category is wrong), plus tasks
assigned to a profile that no longer exists. The worker only discovers this after it has been
spawned, so the dispatcher crash-loops the task. :func:`validate_task_spawnable` answers the
same question up front, from data only:

    result = validate_task_spawnable(task, profiles)
    if not result.spawnable:
        refuse / block with result.reason (machine code) and result.message (human text)
    else:
        persist / pass result.skills (normalized names) instead of the raw names

The function is pure: no filesystem, config, database or environment access. The caller builds
*profiles* (profile id -> skill names that profile can load, disabled skills already removed)
and decides what to do with the verdict (``kanban create`` refuses, the dispatcher blocks).

Skill-name normalization: any category prefix (``category/name``, ``category:name``) is dropped
and the bare name is used when it identifies exactly one skill on the profile, so a known bare
name passes through unchanged and a wrong category no longer matters. Plugin skills, listed in
the catalog as ``namespace:name``, keep their qualified name (the runtime only resolves them
that way). A bare name shared by several skills on the profile is refused as ambiguous unless
the requested qualified name picks exactly one of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

REASON_NO_ASSIGNEE = "preflight:no_assignee"
REASON_UNKNOWN_ASSIGNEE = "preflight:unknown_assignee"
REASON_INVALID_SKILLS = "preflight:invalid_skills"
REASON_UNKNOWN_SKILL = "preflight:unknown_skill"
REASON_AMBIGUOUS_SKILL = "preflight:ambiguous_skill"


@dataclass(frozen=True)
class SpawnCheck:
    """Verdict of :func:`validate_task_spawnable`.

    ``reason`` is ``None`` when spawnable, else one ``preflight:*`` code (the first problem in
    the order no_assignee, unknown_assignee, invalid_skills, unknown_skill, ambiguous_skill);
    ``problems`` lists every problem as text. ``skills`` are the names to persist and pass to
    the worker; ``renamed`` maps each raw name that was normalized to its new name.
    """

    spawnable: bool
    reason: str | None
    message: str
    assignee: str | None
    skills: tuple[str, ...] = ()
    unknown_skills: tuple[str, ...] = ()
    ambiguous_skills: tuple[str, ...] = ()
    renamed: Mapping[str, str] = field(default_factory=dict)
    available_on: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    problems: tuple[str, ...] = ()


def normalize_skill_name(name: str) -> str:
    """Bare skill name: strip whitespace and any ``category/`` or ``namespace:`` prefix."""
    bare = (name or "").strip().replace(":", "/").rstrip("/")
    return bare.rsplit("/", 1)[-1].strip()


def normalize_assignee(name: Any) -> str | None:
    """Profile id as the dispatcher uses it (hermes_cli.profiles.normalize_profile_name rules):
    stripped, ``default`` matched case-insensitively, named profiles lowercase. ``None`` if empty."""
    if not isinstance(name, str) or not name.strip():
        return None
    stripped = name.strip()
    return "default" if stripped.casefold() == "default" else stripped.lower()


def _field(task: Any, key: str) -> Any:
    if isinstance(task, Mapping):
        return task.get(key)
    try:
        return task[key]  # sqlite3.Row and similar
    except (TypeError, KeyError, IndexError):
        return getattr(task, key, None)


def _requested_skills(raw: Any) -> list[str] | None:
    """Skill names from a task field: list/tuple, a single string, a JSON array string (DB
    column) or None. Blank entries are dropped. ``None`` means the value is malformed."""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except ValueError:
                return None
        else:
            raw = [text]
    if not isinstance(raw, (list, tuple)) or not all(isinstance(x, str) for x in raw):
        return None
    return [x.strip() for x in raw if x.strip()]


class _ProfileSkills:
    """Lookup index over one profile's skill names (bare, ``category/name`` or ``plugin:name``)."""

    def __init__(self, names: Iterable[str]):
        self.exact: set[str] = set()
        self.by_bare: dict[str, set[str]] = {}
        for n in names or ():
            n = (n or "").strip() if isinstance(n, str) else ""
            if not n:
                continue
            self.exact.add(n)
            self.by_bare.setdefault(normalize_skill_name(n), set()).add(n)

    def resolve(self, raw: str) -> tuple[str, str | None]:
        """(``ok``|``unknown``|``ambiguous``, name to use).

        Unique bare name -> the bare name (a known bare name therefore stays unchanged), except a
        plugin skill (``namespace:name`` in the catalog), which only resolves qualified. Several
        skills with that bare name -> only an exact qualified request picks one."""
        bare = normalize_skill_name(raw)
        candidates = self.by_bare.get(bare, set())
        if len(candidates) == 1:
            (only,) = candidates
            return "ok", (only if ":" in only else bare)
        if len(candidates) > 1:
            if raw in self.exact:
                return "ok", raw
            slashed = raw.replace(":", "/")
            picked = [c for c in candidates if c.replace(":", "/") == slashed]
            return ("ok", picked[0]) if len(picked) == 1 else ("ambiguous", None)
        return "unknown", None


def validate_task_spawnable(task: Any, profiles: Mapping[str, Iterable[str]]) -> SpawnCheck:
    """Can *task* be spawned on its assignee profile without failing on skills or assignee?

    *task*: mapping, ``sqlite3.Row`` or object with ``assignee`` and ``skills`` (list, JSON array
    string as stored in the ``tasks`` table, single string or ``None``).
    *profiles*: profile id -> the skill names that profile can load. A profile with no skills
    maps to an empty iterable; a profile missing from the mapping does not exist.
    """
    profile_map = {}
    for key, names in (profiles or {}).items():
        canon = normalize_assignee(key)
        if canon is not None:
            profile_map[canon] = _ProfileSkills(names)

    raw_assignee = _field(task, "assignee")
    assignee = normalize_assignee(raw_assignee)
    requested = _requested_skills(_field(task, "skills"))

    if assignee is None:
        msg = "Task has no assignee; the dispatcher never spawns it."
        return SpawnCheck(False, REASON_NO_ASSIGNEE, msg, None, problems=(msg,))
    if assignee not in profile_map:
        msg = f"Unknown assignee profile '{assignee}': no such profile on this install."
        skills = tuple(dict.fromkeys(normalize_skill_name(s) for s in requested or []))
        return SpawnCheck(False, REASON_UNKNOWN_ASSIGNEE, msg, assignee, skills=skills, problems=(msg,))
    if requested is None:
        msg = "Task skills field is malformed: expected a list of skill names."
        return SpawnCheck(False, REASON_INVALID_SKILLS, msg, assignee, problems=(msg,))

    index = profile_map[assignee]
    kept: list[str] = []
    unknown: list[str] = []
    ambiguous: list[str] = []
    renamed: dict[str, str] = {}
    for raw in requested:
        status, name = index.resolve(raw)
        if status == "unknown":
            unknown.append(raw)
        elif status == "ambiguous":
            ambiguous.append(raw)
        else:
            if name != raw:
                renamed[raw] = name
            if name not in kept:
                kept.append(name)

    problems: list[str] = []
    if unknown:
        problems.append(f"Unknown skill(s) for profile '{assignee}': {', '.join(unknown)}")
    if ambiguous:
        problems.append(f"Ambiguous skill name(s) for profile '{assignee}': {', '.join(ambiguous)}"
                        " (several skills share the name; use category/name)")
    available_on = {}
    for raw in unknown:
        where = tuple(sorted(p for p, idx in profile_map.items()
                             if p != assignee and idx.resolve(raw)[0] == "ok"))
        if where:
            available_on[raw] = where
    if problems:
        hints = [f"'{raw}' exists on: {', '.join(where)}" for raw, where in available_on.items()]
        msg = "; ".join(problems + hints)
        reason = REASON_UNKNOWN_SKILL if unknown else REASON_AMBIGUOUS_SKILL
        return SpawnCheck(False, reason, msg, assignee, tuple(kept), tuple(unknown), tuple(ambiguous),
                          renamed, available_on, tuple(problems))
    msg = f"Spawnable on profile '{assignee}'"
    if renamed:
        msg += "; normalized " + ", ".join(f"'{a}' -> '{b}'" for a, b in renamed.items())
    return SpawnCheck(True, None, msg + ".", assignee, tuple(kept), renamed=renamed)

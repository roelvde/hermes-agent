"""Create-time spawnability check: the I/O side of :mod:`hermes_cli.kanban_preflight`.

``kanban create`` (CLI and the ``kanban_create`` tool) call :func:`preflight_create` before the
task row is written. It builds the skill catalog of the assignee profile with the real skill
resolver (``tools.skills_tool._skill_catalog`` under that profile's HERMES_HOME, the catalog
``skill_view`` / the ``--skills`` preload use in the worker), hands it to the pure
:func:`~hermes_cli.kanban_preflight.validate_task_spawnable` and returns the verdict.

The catalog is built in a fresh Python subprocess (``python -m hermes_cli.kanban_preflight_io``),
like the worker itself does. In-process it is not reliable: ``tools.skills_tool`` caches scans,
and ``_skills_dir()`` falls back to a module-level ``SKILLS_DIR`` that ignores the per-profile home
override as soon as anything has patched it (several test suites do), which silently returned the
wrong profile's skills. A subprocess is also free of races with other threads of the gateway.

Policy:
- Nothing to check (no skills requested): returns ``None`` and does no I/O. Assignee existence is
  deliberately not enforced when no skills are pinned; that stays the dispatcher's job.
- Refused: the caller shows :func:`refusal_message` (names the profile and every missing skill,
  with a hint on which profile has it) and does not create the task.
- Spawnable: the caller stores ``check.skills`` (normalized names), not the raw input.
- The catalog cannot be built (subprocess error, timeout): fail open with a warning. A create that
  cannot be verified must not be blocked by an infrastructure problem; the dispatcher keeps its
  own failure path.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from hermes_cli.kanban_preflight import SpawnCheck, normalize_assignee, validate_task_spawnable

logger = logging.getLogger(__name__)

_SENTINEL = "KANBAN_PREFLIGHT_CATALOG_JSON:"
_SUBPROCESS_TIMEOUT_SECONDS = 60


def _profile_home(profile: str) -> Path:
    from hermes_cli.profiles import get_profile_dir

    return Path(get_profile_dir(profile))


def profile_skill_names(profile: str) -> list[str]:
    """Skill names the worker of *profile* can load: ``category/name`` path of every loadable
    catalog entry plus its declared/load name when that differs from the directory name;
    disabled skills left out.

    Run this in a clean process (see module docstring); :func:`profile_catalogs` does.
    Hidden entries (platform/environment gated) are included because ``skill_view``, which the
    ``--skills`` preload uses, resolves them too. Project-tier skills are skipped: they depend on
    the worker's working directory, which is not known at create time."""
    from agent.skill_utils import (
        ESSENTIAL_SKILLS, TIER_PROJECT, is_disabled_entry, parse_config_string_list, yaml_load)
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.skills_tool import _skill_catalog, clear_skills_cache

    home = _profile_home(profile)
    # The profile's own skills.disabled list, read straight from its config.yaml.
    disabled: set[str] = set()
    try:
        cfg = yaml_load((home / "config.yaml").read_text(encoding="utf-8-sig")) or {}
        skills_cfg = cfg.get("skills") if isinstance(cfg, dict) else None
        if isinstance(skills_cfg, dict):
            disabled = {str(n).strip() for n in parse_config_string_list(skills_cfg.get("disabled"))
                        if str(n).strip()}
    except FileNotFoundError:
        pass
    disabled -= set(ESSENTIAL_SKILLS)
    token = set_hermes_home_override(home)
    try:
        clear_skills_cache()
        entries = [e for e in _skill_catalog(skip_disabled=True, include_hidden=True)
                   if not is_disabled_entry(e, disabled)]
    finally:
        reset_hermes_home_override(token)
    names: set[str] = set()
    for entry in entries:
        if entry.get("tier") == TIER_PROJECT or not entry.get("load_name"):
            continue
        rel = str(entry.get("relative_path") or "")
        dir_name = rel.rsplit("/", 1)[-1]
        # One string per distinct bare name: the path (category/name) plus the declared or
        # load name only when it differs from the directory name. Adding both for the same
        # skill would make the validator see two skills sharing one bare name (ambiguous).
        if rel:
            names.add(rel)
        for key in ("load_name", "name"):
            if entry.get(key) and str(entry[key]) != dir_name:
                names.add(str(entry[key]))
        if not rel:
            names.add(str(entry["load_name"]))
    return sorted(names)


def profile_catalogs(profiles: Iterable[str]) -> dict[str, list[str]]:
    """``{profile: skill names}`` for *profiles*, built by one clean subprocess.
    Raises on a subprocess failure, timeout or unparseable output."""
    ids = list(profiles)
    if not ids:
        return {}
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.kanban_preflight_io", *ids],
        cwd=root, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_SECONDS, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(_SENTINEL)]
    if proc.returncode != 0 or not lines:
        raise RuntimeError(f"skill catalog subprocess failed (rc={proc.returncode}): {proc.stderr.strip()[-300:]}")
    return json.loads(lines[-1][len(_SENTINEL):])


def _all_profile_ids() -> list[str]:
    from hermes_cli.profiles import list_profiles

    return [p.name for p in list_profiles(lazy_skill_count=True)]


def build_profiles(assignee: str, *, everyone: bool = False) -> dict[str, list[str]]:
    """Mapping profile id -> skill names for the pure validator. Only the assignee unless
    *everyone* (used to give an 'exists on: ...' hint after a refusal)."""
    if everyone:
        return profile_catalogs(_all_profile_ids())
    from hermes_cli.profiles import profile_exists

    return profile_catalogs([assignee]) if profile_exists(assignee) else {}


def preflight_create(assignee: str | None, skills: Iterable[str] | None) -> SpawnCheck | None:
    """Verdict for a task about to be created, or ``None`` when there is nothing to check
    (no skills) or the check itself could not run (fail open, warning logged)."""
    requested = [s for s in (skills or ()) if isinstance(s, str) and s.strip()]
    if not requested:
        return None
    task = {"assignee": assignee, "skills": requested}
    canon = normalize_assignee(assignee)
    if canon is None:
        return validate_task_spawnable(task, {})
    try:
        check = validate_task_spawnable(task, build_profiles(canon))
        if not check.spawnable and check.reason == "preflight:unknown_skill":
            # Second pass, only on refusal: which other profile has the skill?
            check = validate_task_spawnable(task, build_profiles(canon, everyone=True))
        return check
    except Exception:
        logger.warning("kanban create preflight skipped: could not build the skill catalog",
                       exc_info=True)
        return None


def refusal_message(check: SpawnCheck) -> str:
    """One error line: reason code, profile, missing skill(s) and where they do exist."""
    return f"kanban create refused ({check.reason}): {check.message}"


def _main(argv: list[str]) -> int:
    out = {pid: profile_skill_names(pid) for pid in argv}
    sys.stdout.write("\n" + _SENTINEL + json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))

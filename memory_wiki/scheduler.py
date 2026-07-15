"""Managed Hermes cron integration for dream sweeps.

One opt-in no-agent job runs the ordered light -> REM -> deep sweep.  The
script is intentionally tiny and delegates policy to this plugin's CLI.
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from . import wikifs

JOB_NAME = "memory-wiki-dream-sweep"
SCRIPT_NAME = "memory-wiki-dream-sweep.py"
DEFAULT_SCHEDULE = "0 3 * * *"
LEGACY_JOB_NAMES = frozenset({
    "memory-wiki-dream-light",
    "memory-wiki-dream-rem",
    "memory-wiki-dream-deep",
})

_SCRIPT = '''"""Managed by the memory-wiki plugin. Do not edit."""
from __future__ import annotations

import shutil
import subprocess
import sys


def main() -> int:
    hermes = shutil.which("hermes")
    if not hermes:
        print("memory-wiki dream sweep failed: hermes executable not found", file=sys.stderr)
        return 127
    try:
        result = subprocess.run(
            [hermes, "memory-wiki", "dream", "--phase", "all", "--apply-safe",
             "--scheduled", "--compile", "--quiet"],
            capture_output=True,
            text=True,
            timeout=240,
        )
    except subprocess.TimeoutExpired:
        print("memory-wiki dream sweep failed: timed out", file=sys.stderr)
        return 124
    if result.returncode:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        print(f"memory-wiki dream sweep failed: {detail}", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
'''


def reconcile(cfg, hermes_home, *, enabled=None, schedule=None, jobs_api=None):
    """Create, update, or remove the single managed dream job.

    ``jobs_api`` is an injectable module-shaped object for tests.  Cron imports
    stay lazy so the memory provider remains importable outside Hermes.
    """
    enabled = (bool(getattr(cfg, "dream_schedule_enabled", False))
               if enabled is None else bool(enabled))
    schedule = str(schedule or getattr(
        cfg, "dream_schedule", DEFAULT_SCHEDULE) or DEFAULT_SCHEDULE).strip()
    home = Path(hermes_home).expanduser().resolve()
    script_path = home / "scripts" / SCRIPT_NAME
    api = jobs_api
    if api is None:
        try:
            from cron import jobs as api
        except Exception as exc:
            return _result(enabled, schedule, available=False, error=str(exc))

    try:
        with _store_scope(api, home):
            jobs = list(api.list_jobs(include_disabled=True))
            managed = [job for job in jobs if _is_managed(job)]
            removed = []
            if not enabled:
                for job in managed:
                    if api.remove_job(job["id"]):
                        removed.append(job["id"])
                try:
                    script_path.unlink()
                except FileNotFoundError:
                    pass
                return _result(False, schedule, job=None, removed=removed)

            schedule = validate_schedule(schedule, jobs_api=api)
            _write_script(script_path)
            primary = next((job for job in managed
                            if job.get("name") == JOB_NAME), None)
            primary = primary or (managed[0] if managed else None)
            created = False
            updated = False
            if primary is None:
                primary = api.create_job(
                    prompt="",
                    schedule=schedule,
                    name=JOB_NAME,
                    deliver="local",
                    script=SCRIPT_NAME,
                    no_agent=True,
                )
                created = True
            else:
                changes = _changes(primary, schedule)
                if changes:
                    primary = api.update_job(primary["id"], changes)
                    if primary is None:
                        raise RuntimeError(
                            "managed dream job vanished during update")
                    updated = True
            for job in managed:
                if job["id"] == primary["id"]:
                    continue
                if api.remove_job(job["id"]):
                    removed.append(job["id"])
        return _result(True, schedule, job=_public_job(primary),
                       created=created, updated=updated, removed=removed,
                       script=str(script_path))
    except Exception as exc:
        return _result(enabled, schedule, available=True, error=str(exc),
                       script=str(script_path) if enabled else "")


def status(cfg, *, hermes_home=None, jobs_api=None):
    """Read-only managed-job status."""
    schedule = str(getattr(cfg, "dream_schedule", DEFAULT_SCHEDULE)
                   or DEFAULT_SCHEDULE).strip()
    api = jobs_api
    if api is None:
        try:
            from cron import jobs as api
        except Exception as exc:
            return _result(bool(getattr(cfg, "dream_schedule_enabled", False)),
                           schedule, available=False, error=str(exc))
    try:
        home = Path(hermes_home).expanduser().resolve() \
            if hermes_home is not None else None
        with _store_scope(api, home):
            managed = [job for job in api.list_jobs(include_disabled=True)
                       if _is_managed(job)]
    except Exception as exc:
        return _result(bool(getattr(cfg, "dream_schedule_enabled", False)),
                       schedule, error=str(exc))
    primary = next((job for job in managed if job.get("name") == JOB_NAME),
                   managed[0] if managed else None)
    return _result(bool(getattr(cfg, "dream_schedule_enabled", False)),
                   schedule, job=_public_job(primary),
                   duplicates=max(0, len(managed) - (1 if primary else 0)))


def validate_schedule(schedule, *, jobs_api=None):
    """Require a recurring five-field cron expression."""
    api = jobs_api
    if api is None:
        from cron import jobs as api
    parsed = api.parse_schedule(str(schedule or "").strip())
    if parsed.get("kind") != "cron":
        raise ValueError("dream schedule must be a cron expression")
    return str(parsed.get("display") or schedule).strip()


def _store_scope(api, home):
    use_store = getattr(api, "use_cron_store", None)
    return use_store(home) if home is not None and callable(use_store) \
        else nullcontext()


def _write_script(path):
    current = wikifs.read_text(path)
    if current == _SCRIPT:
        return
    wikifs.atomic_write(path, _SCRIPT)


def _is_managed(job):
    return (str(job.get("name") or "") in ({JOB_NAME} | LEGACY_JOB_NAMES)
            or Path(str(job.get("script") or "")).name == SCRIPT_NAME)


def _changes(job, schedule):
    changes = {}
    current_schedule = str(job.get("schedule_display") or "").strip()
    reviving = (not job.get("enabled", True)
                or job.get("state") == "paused"
                or bool(job.get("paused_at"))
                or bool(job.get("paused_reason")))
    if current_schedule != schedule or reviving:
        changes["schedule"] = schedule
        changes["schedule_display"] = schedule
    desired = {
        "name": JOB_NAME,
        "prompt": "",
        "deliver": "local",
        "script": SCRIPT_NAME,
        "no_agent": True,
        "enabled": True,
        "state": "scheduled",
    }
    for key, value in desired.items():
        if job.get(key) != value:
            changes[key] = value
    if reviving:
        changes["paused_at"] = None
        changes["paused_reason"] = None
    return changes


def _public_job(job):
    if not job:
        return None
    return {key: job.get(key) for key in (
        "id", "name", "schedule_display", "enabled", "state",
        "next_run_at", "last_run_at", "last_status", "last_error")}


def _result(enabled, schedule, *, available=True, job=None, created=False,
            updated=False, removed=None, duplicates=0, script="", error=""):
    return {
        "available": bool(available),
        "enabled": bool(enabled),
        "schedule": schedule,
        "job": job,
        "created": bool(created),
        "updated": bool(updated),
        "removed": list(removed or []),
        "duplicates": int(duplicates),
        "script": script,
        "error": str(error or ""),
    }

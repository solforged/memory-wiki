from types import SimpleNamespace
from contextlib import contextmanager

from memory_wiki import scheduler


class FakeJobs:
    def __init__(self, jobs=None):
        self.jobs = list(jobs or [])
        self.created = []
        self.updated = []
        self.removed = []

    def list_jobs(self, include_disabled=False):
        return list(self.jobs)

    def parse_schedule(self, schedule):
        return {"kind": "cron", "display": schedule}

    def create_job(self, **kwargs):
        job = {"id": "new-job", "schedule_display": kwargs["schedule"],
               "enabled": True, "state": "scheduled", **kwargs}
        self.jobs.append(job)
        self.created.append(kwargs)
        return job

    def update_job(self, job_id, updates):
        self.updated.append((job_id, dict(updates)))
        for job in self.jobs:
            if job["id"] == job_id:
                job.update(updates)
                if isinstance(updates.get("schedule"), str):
                    job["schedule_display"] = updates["schedule"]
                return dict(job)
        return None

    def remove_job(self, job_id):
        self.removed.append(job_id)
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job["id"] != job_id]
        return len(self.jobs) != before


class ScopedJobs(FakeJobs):
    def __init__(self, jobs=None):
        super().__init__(jobs)
        self.scopes = []

    @contextmanager
    def use_cron_store(self, home):
        self.scopes.append(str(home))
        yield


def cfg(**values):
    return SimpleNamespace(dream_schedule_enabled=values.get("enabled", False),
                           dream_schedule=values.get("schedule", "0 3 * * *"))


def test_reconcile_creates_one_no_agent_job(tmp_path):
    jobs = FakeJobs()
    result = scheduler.reconcile(cfg(enabled=True), tmp_path, jobs_api=jobs)

    assert result["created"] is True
    assert result["error"] == ""
    assert jobs.created == [{
        "prompt": "", "schedule": "0 3 * * *",
        "name": scheduler.JOB_NAME, "deliver": "local",
        "script": scheduler.SCRIPT_NAME, "no_agent": True,
    }]
    script = tmp_path / "scripts" / scheduler.SCRIPT_NAME
    assert script.is_file()
    assert '"--phase", "all"' in script.read_text()
    assert '"--scheduled"' in script.read_text()


def test_reconcile_updates_primary_and_removes_duplicates(tmp_path):
    jobs = FakeJobs([
        {"id": "primary", "name": scheduler.JOB_NAME,
         "schedule_display": "0 3 * * *", "script": "old.py",
         "no_agent": False, "deliver": "origin", "enabled": False,
         "state": "paused", "prompt": "old"},
        {"id": "legacy", "name": "memory-wiki-dream-rem",
         "schedule_display": "every 3h"},
    ])

    result = scheduler.reconcile(cfg(enabled=True), tmp_path, jobs_api=jobs)

    assert result["updated"] is True
    assert result["removed"] == ["legacy"]
    assert jobs.jobs[0]["script"] == scheduler.SCRIPT_NAME
    assert jobs.jobs[0]["no_agent"] is True
    assert jobs.jobs[0]["schedule_display"] == "0 3 * * *"
    assert jobs.updated[0][1]["schedule"] == "0 3 * * *"
    assert jobs.updated[0][1]["paused_at"] is None
    assert jobs.updated[0][1]["paused_reason"] is None


def test_reconcile_is_idempotent(tmp_path):
    job = {"id": "primary", "name": scheduler.JOB_NAME,
           "schedule_display": "0 3 * * *", "script": scheduler.SCRIPT_NAME,
           "no_agent": True, "deliver": "local", "enabled": True,
           "state": "scheduled", "prompt": ""}
    jobs = FakeJobs([job])

    first = scheduler.reconcile(cfg(enabled=True), tmp_path, jobs_api=jobs)
    second = scheduler.reconcile(cfg(enabled=True), tmp_path, jobs_api=jobs)

    assert not first["created"] and not first["updated"]
    assert not second["created"] and not second["updated"]
    assert jobs.updated == []


def test_reconcile_rejects_non_cron_before_writing_script(tmp_path):
    jobs = FakeJobs()
    jobs.parse_schedule = lambda schedule: {
        "kind": "interval", "display": "every 120m"}

    result = scheduler.reconcile(
        cfg(enabled=True, schedule="every 2h"), tmp_path, jobs_api=jobs)

    assert "cron expression" in result["error"]
    assert jobs.created == []
    assert not (tmp_path / "scripts" / scheduler.SCRIPT_NAME).exists()


def test_disable_removes_only_managed_jobs_and_script(tmp_path):
    script = tmp_path / "scripts" / scheduler.SCRIPT_NAME
    script.parent.mkdir(parents=True)
    script.write_text("old")
    jobs = FakeJobs([
        {"id": "ours", "name": scheduler.JOB_NAME},
        {"id": "other", "name": "daily report", "script": "report.py"},
    ])

    result = scheduler.reconcile(cfg(), tmp_path, enabled=False, jobs_api=jobs)

    assert result["removed"] == ["ours"]
    assert [job["id"] for job in jobs.jobs] == ["other"]
    assert not script.exists()


def test_status_is_read_only(tmp_path):
    jobs = FakeJobs([{"id": "ours", "name": scheduler.JOB_NAME,
                      "enabled": True, "state": "scheduled",
                      "schedule_display": "0 3 * * *"}])

    result = scheduler.status(cfg(enabled=True), jobs_api=jobs)

    assert result["job"]["id"] == "ours"
    assert jobs.created == [] and jobs.updated == [] and jobs.removed == []


def test_reconcile_and_status_pin_the_requested_profile(tmp_path):
    jobs = ScopedJobs()
    scheduler.reconcile(cfg(enabled=True), tmp_path, jobs_api=jobs)
    scheduler.status(cfg(enabled=True), hermes_home=tmp_path, jobs_api=jobs)

    assert jobs.scopes == [str(tmp_path.resolve()), str(tmp_path.resolve())]

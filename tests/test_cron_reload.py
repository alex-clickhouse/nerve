"""Tests for cron hot-reload and the reserved job-id namespace (Story 5)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import yaml

from nerve.cron.service import CronService, _job_changed
from nerve.cron.jobs import CronJob, is_reserved_job_id


def _write_jobs(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"jobs": jobs}), encoding="utf-8")


@pytest_asyncio.fixture
async def svc(tmp_path):
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True)
    jobs_file = cron_dir / "jobs.yaml"
    _write_jobs(jobs_file, [])

    config = MagicMock()
    config.timezone = "UTC"
    config.cron.jobs_file = jobs_file
    config.cron.system_file = cron_dir / "system.yaml"  # absent
    config.cron.gate_plugins_dir = cron_dir / "gates"    # absent → no-op

    db = AsyncMock()
    db.get_last_successful_cron_run = AsyncMock(return_value=None)

    service = CronService(config, AsyncMock(), db)
    # Start the scheduler paused so add/remove/replace_existing hit the real
    # jobstore (as in production) without actually firing jobs. An unstarted
    # scheduler keeps jobs in a "pending" list where replace_existing is a no-op.
    service.scheduler.start(paused=True)
    try:
        yield service, jobs_file
    finally:
        service.scheduler.shutdown(wait=False)


def _job_dict(job_id="j1", schedule="1h", **kw):
    return {"id": job_id, "schedule": schedule, "prompt": "do stuff", **kw}


class TestReload:
    @pytest.mark.asyncio
    async def test_add_job(self, svc):
        service, jobs_file = svc
        result = await service.reload()  # empty → nothing
        assert result["added"] == [] and result["enabled"] == 0

        _write_jobs(jobs_file, [_job_dict("j1")])
        result = await service.reload()
        assert result["added"] == ["j1"]
        assert service.scheduler.get_job("j1") is not None
        assert result["enabled"] == 1

    @pytest.mark.asyncio
    async def test_remove_job(self, svc):
        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("j1")])
        await service.reload()
        assert service.scheduler.get_job("j1") is not None

        _write_jobs(jobs_file, [])
        result = await service.reload()
        assert result["removed"] == ["j1"]
        assert service.scheduler.get_job("j1") is None

    @pytest.mark.asyncio
    async def test_disable_job_unschedules(self, svc):
        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("j1")])
        await service.reload()

        _write_jobs(jobs_file, [_job_dict("j1", enabled=False)])
        result = await service.reload()
        # Still present in config (so not "removed"), but unscheduled + not enabled.
        assert result["removed"] == []
        assert "j1" not in result["added"] and "j1" not in result["updated"]
        assert service.scheduler.get_job("j1") is None
        assert result["enabled"] == 0

    @pytest.mark.asyncio
    async def test_reenable_job(self, svc):
        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("j1", enabled=False)])
        await service.reload()
        assert service.scheduler.get_job("j1") is None

        _write_jobs(jobs_file, [_job_dict("j1", enabled=True)])
        result = await service.reload()
        assert result["added"] == ["j1"]
        assert service.scheduler.get_job("j1") is not None

    @pytest.mark.asyncio
    async def test_reschedule_job_reports_updated(self, svc):
        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("j1", schedule="1h")])
        await service.reload()
        first_trigger = str(service.scheduler.get_job("j1").trigger)

        _write_jobs(jobs_file, [_job_dict("j1", schedule="0 3 * * *")])
        result = await service.reload()
        assert result["updated"] == ["j1"]
        assert str(service.scheduler.get_job("j1").trigger) != first_trigger

    @pytest.mark.asyncio
    async def test_unchanged_job_is_noop(self, svc):
        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("j1")])
        await service.reload()

        result = await service.reload()  # identical file
        assert result == {"added": [], "removed": [], "updated": [], "enabled": 1}
        assert service.scheduler.get_job("j1") is not None


class TestReloadSafety:
    @pytest.mark.asyncio
    async def test_new_arrives_disabled_is_noop(self, svc):
        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("j1", enabled=False)])
        result = await service.reload()
        assert result == {"added": [], "removed": [], "updated": [], "enabled": 0}
        assert service.scheduler.get_job("j1") is None

    @pytest.mark.asyncio
    async def test_disabled_stays_disabled_no_remove(self, svc):
        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("j1", enabled=False)])
        await service.reload()
        # Reload again, still disabled — must not try to remove a job that was
        # never scheduled (the get_job guard), and must not report it removed.
        result = await service.reload()
        assert result["removed"] == []

    @pytest.mark.asyncio
    async def test_description_only_change_is_noop(self, svc):
        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("j1", description="old")])
        await service.reload()
        _write_jobs(jobs_file, [_job_dict("j1", description="new label")])
        result = await service.reload()
        # A pure label edit must not reschedule (which would reset the timer).
        assert result["updated"] == []

    @pytest.mark.asyncio
    async def test_reserved_id_collision_ignored(self, svc):
        service, jobs_file = svc
        # Register a fake source runner id + rely on cleanup/wakeup_sweep.
        fake_runner = MagicMock()
        fake_runner.job_id = "source:gmail"
        service._source_runners = [fake_runner]
        # Simulate the internal cleanup job already occupying the slot.
        service.scheduler.add_job(
            lambda: None, "interval", seconds=3600, id="cleanup",
        )

        _write_jobs(jobs_file, [
            _job_dict("cleanup", schedule="1h"),
            _job_dict("source:gmail", schedule="1h"),
            _job_dict("legit", schedule="1h"),
        ])
        result = await service.reload()
        # Only the non-reserved job is scheduled; reserved ids are ignored.
        assert result["added"] == ["legit"]
        assert service.scheduler.get_job("legit") is not None
        # The internal cleanup job is untouched (still the lambda interval job).
        assert service.scheduler.get_job("cleanup") is not None

    @pytest.mark.asyncio
    async def test_reserved_removal_does_not_delete_internal_job(self, svc):
        service, jobs_file = svc
        # User previously had a 'cleanup' cron in self._jobs (as start would keep).
        service._jobs = [CronJob(id="cleanup", schedule="1h", prompt="x")]
        service.scheduler.add_job(
            lambda: None, "interval", seconds=3600, id="cleanup",
        )
        _write_jobs(jobs_file, [])  # user removed their cleanup cron
        result = await service.reload()
        # Internal cleanup job must survive; not reported removed.
        assert "cleanup" not in result["removed"]
        assert service.scheduler.get_job("cleanup") is not None

    @pytest.mark.asyncio
    async def test_source_namespace_reserved_while_runner_is_down(self, svc):
        service, jobs_file = svc
        # No source runners registered at all — the whole `source:` namespace is
        # still off limits, otherwise the job would be silently replaced the
        # moment that source is turned back on.
        assert service._source_runners == []

        _write_jobs(jobs_file, [
            _job_dict("source:telegram", schedule="1h"),
            _job_dict("legit", schedule="1h"),
        ])
        result = await service.reload()
        assert result["added"] == ["legit"]
        assert result["enabled"] == 1
        assert service.scheduler.get_job("source:telegram") is None
        assert "source:telegram" not in [j.id for j in service._jobs]

    @pytest.mark.asyncio
    async def test_reserved_dropped_on_the_merged_path(self, svc):
        """The system+user merge — every install after `nerve init` — filters too."""
        service, jobs_file = svc
        system_file = service.config.cron.system_file
        _write_jobs(system_file, [
            _job_dict("wakeup_sweep", schedule="1h"),
            _job_dict("sys-job", schedule="1h"),
        ])
        _write_jobs(jobs_file, [
            _job_dict("source:github", schedule="1h"),
            _job_dict("user-job", schedule="1h"),
        ])
        result = await service.reload()
        assert sorted(result["added"]) == ["sys-job", "user-job"]
        assert sorted(j.id for j in service._jobs) == ["sys-job", "user-job"]
        assert service.scheduler.get_job("source:github") is None
        # The daemon's own wakeup sweep is never displaced by a same-named job.
        assert service.scheduler.get_job("wakeup_sweep") is None

    @pytest.mark.asyncio
    async def test_reserved_rejection_is_logged(self, svc, caplog):
        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("source:gmail", schedule="1h")])
        with caplog.at_level(logging.WARNING, logger="nerve.cron.service"):
            await service.reload()
        # An operator who hand-edited jobs.yaml must be able to find out why.
        assert any(
            "source:gmail" in r.getMessage() and "reserved" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_unchanged_job_with_missing_trigger_is_rescheduled(self, svc):
        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("j1")])
        await service.reload()

        # Something dropped the trigger behind reload's back. An unchanged job
        # must not be left stranded while the summary keeps calling it enabled.
        service.scheduler.remove_job("j1")
        result = await service.reload()
        # Newly scheduled, so "added" — the summary reports what the scheduler
        # did, not what changed in the file.
        assert result["added"] == ["j1"]
        assert result["updated"] == []
        assert result["enabled"] == 1
        assert service.scheduler.get_job("j1") is not None

    @pytest.mark.asyncio
    async def test_jobs_refreshed_without_scheduler_report_as_added(self, svc):
        service, jobs_file = svc
        # run_job()/rotate_session() refresh _jobs from disk without touching the
        # scheduler. The next reload schedules those jobs for the first time, so
        # they are added, not updated.
        _write_jobs(jobs_file, [_job_dict("j1"), _job_dict("j2")])
        service._jobs = service._load_merged_jobs()
        assert service.scheduler.get_job("j1") is None

        result = await service.reload()
        assert sorted(result["added"]) == ["j1", "j2"]
        assert result["updated"] == []

    @pytest.mark.asyncio
    async def test_malformed_yaml_refused(self, svc):
        from nerve.config import ConfigError

        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("j1")])
        await service.reload()

        # Corrupt the file — reload must refuse and leave the schedule intact.
        jobs_file.write_text("jobs: [ this is: not valid: yaml\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            await service.reload()
        assert service.scheduler.get_job("j1") is not None  # still scheduled

    @pytest.mark.asyncio
    async def test_trigger_build_failure_leaves_schedule_intact(self, svc):
        """A job whose trigger won't build must not take the others down with it.

        Interval jobs anchor their timer to the last successful run, so building
        a trigger reads the database — and that read can fail long after the
        config parsed fine. If the reload had already unscheduled the removed
        jobs by then, the daemon would be left half-reloaded with no way for an
        operator to tell which crons are still live.
        """
        service, jobs_file = svc
        _write_jobs(jobs_file, [
            _job_dict("keep-a", schedule="1h"),
            _job_dict("keep-b", schedule="30m"),
            _job_dict("doomed", schedule="2h"),
        ])
        await service.reload()
        before = {
            jid: str(service.scheduler.get_job(jid).trigger)
            for jid in ("keep-a", "keep-b", "doomed")
        }

        # A reload that touches all three paths: keep-a rescheduled, keep-b
        # removed, doomed's trigger unbuildable.
        _write_jobs(jobs_file, [
            _job_dict("keep-a", schedule="15m"),
            _job_dict("doomed", schedule="3h"),
        ])

        async def flaky_last_run(job_id):
            if job_id == "doomed":
                raise RuntimeError("database is locked")
            return None

        service.db.get_last_successful_cron_run = AsyncMock(
            side_effect=flaky_last_run,
        )

        with pytest.raises(RuntimeError):
            await service.reload()

        # Every job that was running still runs, on its original trigger.
        live = {jid: service.scheduler.get_job(jid) for jid in before}
        assert [jid for jid, j in live.items() if j is None] == []
        assert {jid: str(j.trigger) for jid, j in live.items()} == before
        # And the recorded job list still matches the live schedule.
        assert sorted(j.id for j in service._jobs) == [
            "doomed", "keep-a", "keep-b",
        ]

    @pytest.mark.asyncio
    async def test_invalid_job_entry_refused(self, svc):
        from nerve.config import ConfigError

        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("j1")])
        await service.reload()
        # A job missing required fields (no prompt/prompt_file) → strict raise.
        jobs_file.write_text(
            yaml.safe_dump({"jobs": [{"id": "bad", "schedule": "1h"}]}),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            await service.reload()
        assert service.scheduler.get_job("j1") is not None

    @pytest.mark.asyncio
    async def test_invalid_schedule_refused(self, svc):
        """A crontab the scheduler rejects refuses the reload, like bad YAML.

        It used to be accepted as a 2h interval, so the reload "succeeded" and
        the job ran on a cadence the operator never wrote down.
        """
        from nerve.config import ConfigError

        service, jobs_file = svc
        _write_jobs(jobs_file, [_job_dict("j1", schedule="1h")])
        await service.reload()
        before = str(service.scheduler.get_job("j1").trigger)

        _write_jobs(jobs_file, [
            _job_dict("j1", schedule="30m"),
            _job_dict("typo", schedule="99 * * * *"),
        ])
        with pytest.raises(ConfigError) as ei:
            await service.reload()

        assert "typo" in str(ei.value)
        # All-or-nothing: j1 keeps its old trigger, the typo is never scheduled.
        assert str(service.scheduler.get_job("j1").trigger) == before
        assert service.scheduler.get_job("typo") is None


class TestInvalidScheduleAtStartup:
    """Startup answers the same error differently from reload(), on purpose."""

    @pytest.mark.asyncio
    async def test_start_skips_only_the_offending_job(
        self, tmp_path, monkeypatch, caplog,
    ):
        """One typo must not cost the other jobs their daemon."""
        import nerve.sources.registry as registry

        monkeypatch.setattr(registry, "build_source_runners", lambda *a, **k: [])

        cron_dir = tmp_path / "cron"
        jobs_file = cron_dir / "jobs.yaml"
        _write_jobs(jobs_file, [
            _job_dict("typo", schedule="99 * * * *"),
            _job_dict("legit", schedule="1h"),
        ])

        config = MagicMock()
        config.timezone = "UTC"
        config.cron.jobs_file = jobs_file
        config.cron.system_file = cron_dir / "system.yaml"  # absent
        config.cron.gate_plugins_dir = cron_dir / "gates"   # absent → no-op

        db = AsyncMock()
        db.get_last_successful_cron_run = AsyncMock(return_value=None)

        service = CronService(config, AsyncMock(), db)
        with caplog.at_level(logging.ERROR, logger="nerve.cron.service"):
            await service.start()  # must not raise
        try:
            assert service.scheduler.get_job("legit") is not None
            assert service.scheduler.get_job("typo") is None
            # Kept in _jobs so list_jobs still shows it — with no next run —
            # rather than dropping it out of sight.
            assert sorted(j.id for j in service._jobs) == ["legit", "typo"]
        finally:
            service.scheduler.shutdown(wait=False)

        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "typo" in logged and "99 * * * *" in logged

    @pytest.mark.asyncio
    async def test_start_skips_only_the_offending_source(
        self, tmp_path, monkeypatch, caplog,
    ):
        """Same for sync.<source>.schedule, and the later sources still register.

        The registration loop sits inside a blanket `except Exception`, so an
        error escaping one runner would silently abandon every runner after it.
        """
        import nerve.sources.registry as registry

        def _runner(name):
            runner = MagicMock()
            runner.source.source_name = name
            runner.job_id = f"source:{name}"
            return runner

        # Bad one first: whatever follows it is what a swallowed error costs.
        monkeypatch.setattr(
            registry, "build_source_runners",
            lambda *a, **k: [_runner("gmail"), _runner("slack")],
        )

        cron_dir = tmp_path / "cron"
        jobs_file = cron_dir / "jobs.yaml"
        _write_jobs(jobs_file, [])

        config = MagicMock()
        config.timezone = "UTC"
        config.cron.jobs_file = jobs_file
        config.cron.system_file = cron_dir / "system.yaml"  # absent
        config.cron.gate_plugins_dir = cron_dir / "gates"   # absent → no-op
        config.sync.gmail.schedule = "0 99 * * *"
        config.sync.slack.schedule = "*/15 * * * *"

        db = AsyncMock()
        db.get_last_successful_cron_run = AsyncMock(return_value=None)

        service = CronService(config, AsyncMock(), db)
        with caplog.at_level(logging.ERROR, logger="nerve.cron.service"):
            await service.start()
        try:
            assert service.scheduler.get_job("source:gmail") is None
            assert service.scheduler.get_job("source:slack") is not None
        finally:
            service.scheduler.shutdown(wait=False)

        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "gmail" in logged and "0 99 * * *" in logged


class TestReloadRoute:
    @pytest.mark.asyncio
    async def test_503_when_no_service(self, monkeypatch):
        import nerve.gateway.server as srv
        from fastapi import HTTPException

        from nerve.gateway.routes.cron import reload_cron_jobs

        monkeypatch.setattr(srv, "_cron_service", None, raising=False)
        with pytest.raises(HTTPException) as ei:
            await reload_cron_jobs(user={})
        assert ei.value.status_code == 503

    @pytest.mark.asyncio
    async def test_returns_summary(self, monkeypatch):
        import nerve.gateway.server as srv

        from nerve.gateway.routes.cron import reload_cron_jobs

        fake = MagicMock()
        fake.reload = AsyncMock(
            return_value={"added": ["a"], "removed": [], "updated": [], "enabled": 1}
        )
        monkeypatch.setattr(srv, "_cron_service", fake, raising=False)
        result = await reload_cron_jobs(user={})
        assert result["reloaded"] is True
        assert result["added"] == ["a"]

    @pytest.mark.asyncio
    async def test_400_on_config_error(self, monkeypatch):
        import nerve.gateway.server as srv
        from fastapi import HTTPException

        from nerve.config import ConfigError
        from nerve.gateway.routes.cron import reload_cron_jobs

        fake = MagicMock()
        fake.reload = AsyncMock(side_effect=ConfigError("bad cron file"))
        monkeypatch.setattr(srv, "_cron_service", fake, raising=False)
        with pytest.raises(HTTPException) as ei:
            await reload_cron_jobs(user={})
        assert ei.value.status_code == 400
        assert "bad cron file" in ei.value.detail

    @pytest.mark.asyncio
    async def test_400_on_invalid_schedule(self, monkeypatch):
        """A typo'd schedule is a bad request, not a server error."""
        import nerve.gateway.server as srv
        from fastapi import HTTPException

        from nerve.cron.service import InvalidScheduleError
        from nerve.gateway.routes.cron import reload_cron_jobs

        fake = MagicMock()
        fake.reload = AsyncMock(
            side_effect=InvalidScheduleError("Cron job 'typo': bad minute"),
        )
        monkeypatch.setattr(srv, "_cron_service", fake, raising=False)
        with pytest.raises(HTTPException) as ei:
            await reload_cron_jobs(user={})
        assert ei.value.status_code == 400
        assert "typo" in ei.value.detail


class TestReservedIds:
    @pytest.mark.parametrize("job_id", [
        "cleanup", "wakeup_sweep",
        "source:gmail", "source:telegram", "source:gmail:me@example.com",
        "source:",
    ])
    def test_reserved(self, job_id):
        assert is_reserved_job_id(job_id)

    @pytest.mark.parametrize("job_id", [
        "cleanup-inbox", "my_wakeup_sweep", "sources:gmail", "source-gmail",
        "morning-briefing",
    ])
    def test_not_reserved(self, job_id):
        assert not is_reserved_job_id(job_id)

    @pytest.mark.asyncio
    async def test_start_drops_reserved_jobs(self, tmp_path, monkeypatch, caplog):
        """Startup must reject reserved ids too, not just reload."""
        import nerve.sources.registry as registry

        monkeypatch.setattr(registry, "build_source_runners", lambda *a, **k: [])

        cron_dir = tmp_path / "cron"
        jobs_file = cron_dir / "jobs.yaml"
        _write_jobs(jobs_file, [
            _job_dict("cleanup", schedule="1h"),
            _job_dict("source:gmail", schedule="1h"),
            _job_dict("legit", schedule="1h"),
        ])

        config = MagicMock()
        config.timezone = "UTC"
        config.cron.jobs_file = jobs_file
        config.cron.system_file = cron_dir / "system.yaml"  # absent
        config.cron.gate_plugins_dir = cron_dir / "gates"   # absent → no-op

        db = AsyncMock()
        db.get_last_successful_cron_run = AsyncMock(return_value=None)

        service = CronService(config, AsyncMock(), db)
        with caplog.at_level(logging.WARNING, logger="nerve.cron.service"):
            await service.start()
        try:
            assert [j.id for j in service._jobs] == ["legit"]
            # No user job in the source namespace, and the daemon's own cleanup
            # job — not the user's — owns the 'cleanup' slot.
            assert service.scheduler.get_job("source:gmail") is None
            assert service.scheduler.get_job("cleanup").name == "Cleanup expired data"
            assert service.scheduler.get_job("legit") is not None
        finally:
            service.scheduler.shutdown(wait=False)

        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "cleanup" in logged and "source:gmail" in logged

    def test_source_runner_ids_are_always_inside_the_namespace(self):
        """The reservation only holds if every runner really lives under it."""
        import inspect

        from nerve.sources.runner import SourceRunner

        # No caller may hand a runner an id outside the reserved namespace.
        assert "job_id" not in inspect.signature(SourceRunner.__init__).parameters

        source = MagicMock()
        source.source_name = "gmail:me@example.com"
        runner = SourceRunner(source=source, db=AsyncMock())
        assert runner.job_id == "source:gmail:me@example.com"
        assert is_reserved_job_id(runner.job_id)


class TestReservedIdsInCli:
    """The daemon-log warning never reaches someone running the CLI."""

    @staticmethod
    def _config_dir(tmp_path, system_jobs, user_jobs):
        cron_dir = tmp_path / "cron"
        _write_jobs(cron_dir / "system.yaml", system_jobs)
        _write_jobs(cron_dir / "jobs.yaml", user_jobs)
        (tmp_path / "config.yaml").write_text(
            f"cron:\n"
            f"  system_file: {cron_dir / 'system.yaml'}\n"
            f"  jobs_file: {cron_dir / 'jobs.yaml'}\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_cron_listing_flags_reserved_jobs(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        import nerve.agent.engine as engine_mod
        import nerve.db as db_mod
        from nerve.cli import main

        # The listing itself needs neither an engine nor a database.
        monkeypatch.setattr(db_mod, "init_db", AsyncMock(return_value=AsyncMock()))
        monkeypatch.setattr(db_mod, "close_db", AsyncMock())
        monkeypatch.setattr(
            engine_mod, "AgentEngine", MagicMock(return_value=AsyncMock()),
        )

        cfg = self._config_dir(
            tmp_path, [], [_job_dict("source:rss"), _job_dict("legit")],
        )
        result = CliRunner().invoke(main, ["-c", str(cfg), "cron"])
        assert result.exit_code == 0, result.output
        # The job is listed (so the reader can see it was read at all) but is
        # never described as enabled.
        assert "source:rss" in result.output
        assert "RESERVED ID" in result.output
        assert "source:rss: 1h (enabled)" not in result.output
        assert "legit: 1h (enabled)" in result.output

    def test_doctor_excludes_reserved_from_the_enabled_count(self, tmp_path):
        from click.testing import CliRunner

        from nerve.cli import main

        cfg = self._config_dir(
            tmp_path, [], [_job_dict("source:rss"), _job_dict("legit")],
        )
        # doctor exits non-zero on this bare config (no API key, no workspace);
        # only its cron reporting is under test here.
        result = CliRunner().invoke(main, ["-c", str(cfg), "doctor"])
        assert "Cron jobs: 1/1 enabled" in result.output
        assert "source:rss" in result.output
        assert "reserved" in result.output


class TestJobChanged:
    def test_detects_schedule_change(self):
        a = CronJob(id="j", schedule="1h", prompt="x")
        b = CronJob(id="j", schedule="2h", prompt="x")
        assert _job_changed(a, b)

    def test_detects_prompt_change(self):
        a = CronJob(id="j", schedule="1h", prompt="x")
        b = CronJob(id="j", schedule="1h", prompt="y")
        assert _job_changed(a, b)

    def test_ignores_metadata_and_identical(self):
        a = CronJob(id="j", schedule="1h", prompt="x")
        b = CronJob(id="j", schedule="1h", prompt="x")
        a.metadata["_source"] = "system"
        b.metadata["_source"] = "user"
        assert not _job_changed(a, b)

    def test_detects_run_if_change(self):
        a = CronJob(id="j", schedule="1h", prompt="x", run_if=[{"type": "tasks"}])
        b = CronJob(id="j", schedule="1h", prompt="x", run_if=[])
        assert _job_changed(a, b)

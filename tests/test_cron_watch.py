"""Tests for CronService.watch_config — auto-reload on file change."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import watchfiles
from watchfiles import Change

from nerve.config import CronConfig, load_config
from nerve.cron import service as cron_service
from nerve.cron.service import CronService, _wait_or_stop
from nerve.utils.aio import stop_background_task

_RELOADED = {"added": [], "updated": [], "removed": [], "enabled": 0}


def _make_service(tmp_path, *, existing_cron_dir=True):
    cron_dir = tmp_path / "cron"
    if existing_cron_dir:
        cron_dir.mkdir(parents=True)
    config = MagicMock()
    config.timezone = "UTC"
    config.cron.jobs_file = cron_dir / "jobs.yaml"
    config.cron.system_file = cron_dir / "system.yaml"
    config.cron.gate_plugins_dir = cron_dir / "gates"
    return CronService(config, AsyncMock(), AsyncMock())


def _point_at(service, cron_dir: Path) -> None:
    service.config.cron.jobs_file = cron_dir / "jobs.yaml"
    service.config.cron.system_file = cron_dir / "system.yaml"
    service.config.cron.gate_plugins_dir = cron_dir / "gates"


async def _until(predicate, *, timeout=10.0):
    """Poll ``predicate`` until it holds, failing the test if it never does."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(0.1)


def _fake_awatch(*batches, seen=None):
    """An awatch stand-in that yields ``batches`` and then stops the watcher.

    Setting the stop event rather than just returning is what lets
    ``watch_config`` — which re-enters awatch until told to stop — finish
    without a sleep-based race.
    """
    async def _gen(*dirs, stop_event=None, **kwargs):
        if seen is not None:
            seen.append(dirs)
        for b in batches:
            yield b
        stop_event.set()
    return _gen


class TestWaitOrStop:
    @pytest.mark.asyncio
    async def test_returns_when_the_event_is_set(self):
        event = asyncio.Event()
        event.set()
        # Would hang for a minute if the event were ignored.
        await asyncio.wait_for(_wait_or_stop(event, 60), timeout=2)

    @pytest.mark.asyncio
    async def test_returns_when_the_timeout_expires(self):
        await asyncio.wait_for(_wait_or_stop(asyncio.Event(), 0.01), timeout=2)


class TestWatchDirs:
    def test_lists_only_existing_dirs(self, tmp_path):
        service = _make_service(tmp_path, existing_cron_dir=True)
        dirs = service._watch_dirs()
        assert str(tmp_path / "cron") in dirs

    def test_falls_back_to_parent_when_cron_dir_absent(self, tmp_path):
        # cron dir doesn't exist yet, but its parent (tmp_path) does.
        service = _make_service(tmp_path, existing_cron_dir=False)
        dirs = service._watch_dirs()
        assert str(tmp_path) in dirs  # watches the parent so cron/ is covered later

    def test_empty_when_not_even_parent_exists(self, tmp_path):
        service = _make_service(tmp_path, existing_cron_dir=False)
        # Point cron files two levels below a non-existent dir.
        _point_at(service, tmp_path / "gone" / "cron")
        assert service._watch_dirs() == []


class TestWatchFilter:
    def test_cron_dir_watch_keeps_the_default_ignores(self, tmp_path):
        service = _make_service(tmp_path)
        cron = tmp_path / "cron"
        accept = service._watch_filter(service._watch_dirs())
        assert accept(Change.modified, str(cron / "jobs.yaml"))
        # Loading a gate plugin writes bytecode next to it; reloading on that
        # would make every reload trigger the next one.
        assert not accept(
            Change.added, str(cron / "gates" / "__pycache__" / "g.pyc")
        )

    def test_parent_fallback_ignores_unrelated_siblings(self, tmp_path):
        service = _make_service(tmp_path, existing_cron_dir=False)
        dirs = service._watch_dirs()
        assert dirs == [str(tmp_path)]  # watching the parent, not the cron dir
        accept = service._watch_filter(dirs)
        cron = tmp_path / "cron"
        assert accept(Change.added, str(cron))  # the cron dir appearing
        assert accept(Change.modified, str(cron / "jobs.yaml"))
        assert not accept(Change.modified, str(tmp_path / "unrelated.yaml"))

    def test_a_modified_directory_is_not_a_change(self, tmp_path):
        """Writing __pycache__ bumps the mtime of the directory holding the
        plugin. The .pyc itself is ignored, but that bump is reported
        separately and would let a reload re-trigger itself."""
        service = _make_service(tmp_path)
        gates = tmp_path / "cron" / "gates"
        gates.mkdir()
        accept = service._watch_filter(service._watch_dirs())
        assert not accept(Change.modified, str(gates))
        # Only the redundant mtime bump goes: a directory appearing or
        # disappearing is real news, and so is any file event.
        assert accept(Change.added, str(tmp_path / "cron" / "new_dir"))
        assert accept(Change.deleted, str(gates))
        assert accept(Change.modified, str(gates / "myplugin.py"))

    def test_targets_are_reread_per_change_not_captured(self, tmp_path):
        """Two missing cron dirs under the same parent give the same watched
        set, so a filter that captured its targets would keep admitting the
        old location and rejecting the new one."""
        service = _make_service(tmp_path, existing_cron_dir=False)
        dirs = service._watch_dirs()
        accept = service._watch_filter(dirs)
        old, new = tmp_path / "cron", tmp_path / "cron2"
        assert accept(Change.added, str(old / "jobs.yaml"))

        _point_at(service, new)
        assert service._watch_dirs() == dirs  # same watch, different targets
        assert accept(Change.added, str(new / "jobs.yaml"))
        assert not accept(Change.added, str(old / "jobs.yaml"))


class TestWatchConfig:
    @pytest.mark.asyncio
    async def test_each_change_batch_triggers_reload(self, tmp_path, monkeypatch):
        service = _make_service(tmp_path)
        calls = []

        async def fake_reload():
            calls.append(1)
            return _RELOADED

        monkeypatch.setattr(service, "reload", fake_reload)
        monkeypatch.setattr(
            watchfiles, "awatch",
            _fake_awatch({("modified", "a")}, {("modified", "b")}),
        )
        await service.watch_config()
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_idle_batches_do_not_reload(self, tmp_path, monkeypatch):
        """awatch is asked to yield on its idle timeout so the watched dirs get
        re-checked; those empty batches are not a reason to reload."""
        service = _make_service(tmp_path)
        reload_mock = AsyncMock(return_value=_RELOADED)
        monkeypatch.setattr(service, "reload", reload_mock)
        monkeypatch.setattr(
            watchfiles, "awatch",
            _fake_awatch(set(), {("modified", "a")}, set()),
        )
        await service.watch_config()
        assert reload_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_first_watch_does_not_reload_on_its_own(self, tmp_path, monkeypatch):
        """start() has just read these files; entering the watch is no reason
        to read them again."""
        service = _make_service(tmp_path)
        reload_mock = AsyncMock(return_value=_RELOADED)
        monkeypatch.setattr(service, "reload", reload_mock)
        monkeypatch.setattr(watchfiles, "awatch", _fake_awatch(set()))
        await service.watch_config()
        reload_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reloads_when_a_new_directory_comes_into_view(
        self, tmp_path, monkeypatch
    ):
        """Whatever is already in a directory when the watch starts raises no
        event, so moving onto one has to reload instead of waiting."""
        service = _make_service(tmp_path, existing_cron_dir=False)
        missing = tmp_path / "gone" / "cron"
        _point_at(service, missing)
        reload_mock = AsyncMock(return_value=_RELOADED)
        monkeypatch.setattr(service, "reload", reload_mock)

        async def fake_wait(event, seconds):
            missing.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(cron_service, "_wait_or_stop", fake_wait)
        # Only idle batches: the reload has to come from the move itself.
        monkeypatch.setattr(watchfiles, "awatch", _fake_awatch(set()))
        await service.watch_config()
        assert reload_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_rewatches_when_the_config_paths_move(self, tmp_path, monkeypatch):
        """A config change that repoints the cron files must re-target the
        watch — otherwise reload() reads one directory while the watcher
        watches another, and auto-reload silently stops firing."""
        service = _make_service(tmp_path)
        moved = tmp_path / "moved"
        moved.mkdir()
        seen: list[tuple[str, ...]] = []

        async def fake_reload():
            _point_at(service, moved)
            return _RELOADED

        monkeypatch.setattr(service, "reload", fake_reload)

        async def fake_awatch(*dirs, stop_event=None, **kwargs):
            seen.append(dirs)
            if len(seen) == 1:
                yield {(Change.modified, "jobs.yaml")}  # reload repoints cron
            else:
                stop_event.set()
                return
                yield  # pragma: no cover — keeps this an async generator

        monkeypatch.setattr(watchfiles, "awatch", fake_awatch)
        await service.watch_config()
        assert seen == [(str(tmp_path / "cron"),), (str(moved),)]

    @pytest.mark.asyncio
    async def test_missing_cron_dir_is_waited_for_not_fatal(
        self, tmp_path, monkeypatch
    ):
        """With no directory to watch at all, the watcher must keep looking
        instead of disabling itself until the next daemon restart."""
        service = _make_service(tmp_path, existing_cron_dir=False)
        missing = tmp_path / "gone" / "cron"
        _point_at(service, missing)
        reload_mock = AsyncMock(return_value=_RELOADED)
        monkeypatch.setattr(service, "reload", reload_mock)
        seen: list[tuple[str, ...]] = []
        waits = []

        async def fake_wait(event, seconds):
            waits.append((event, seconds))
            if len(waits) == 2:
                missing.mkdir(parents=True)  # the dir shows up while we wait

        monkeypatch.setattr(cron_service, "_wait_or_stop", fake_wait)
        monkeypatch.setattr(watchfiles, "awatch", _fake_awatch(set(), seen=seen))
        await service.watch_config()
        assert [s for _, s in waits] == [cron_service._WATCH_RETRY_SECONDS] * 2
        # It waits on the same event that shuts the watcher down, so a stop
        # doesn't have to sit out the poll interval.
        assert all(isinstance(e, asyncio.Event) for e, _ in waits)
        assert seen == [(str(missing),)]
        assert reload_mock.await_count == 1  # the dir it moved onto is unread

    @pytest.mark.asyncio
    async def test_stop_event_interrupts_the_wait_for_a_missing_dir(
        self, tmp_path, monkeypatch
    ):
        service = _make_service(tmp_path, existing_cron_dir=False)
        _point_at(service, tmp_path / "gone" / "cron")
        monkeypatch.setattr(cron_service, "_WATCH_RETRY_SECONDS", 60)
        waiting = asyncio.Event()
        real_wait = cron_service._wait_or_stop

        async def instrumented(event, seconds):
            waiting.set()
            await real_wait(event, seconds)

        monkeypatch.setattr(cron_service, "_wait_or_stop", instrumented)
        stop = asyncio.Event()
        task = asyncio.create_task(service.watch_config(stop_event=stop))
        await asyncio.wait_for(waiting.wait(), timeout=2)
        stop.set()
        await asyncio.wait_for(task, timeout=2)  # not stuck in the 60s wait

    @pytest.mark.asyncio
    async def test_survives_reload_error_and_keeps_watching(self, tmp_path, monkeypatch):
        service = _make_service(tmp_path)
        calls = []

        async def flaky_reload():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("malformed file mid-edit")
            return _RELOADED

        monkeypatch.setattr(service, "reload", flaky_reload)
        monkeypatch.setattr(
            watchfiles, "awatch",
            _fake_awatch({("modified", "a")}, {("modified", "b")}),
        )
        await service.watch_config()
        # First reload raised; the watcher kept going and handled the second.
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_a_failed_reload_does_not_discard_the_catch_up(
        self, tmp_path, monkeypatch
    ):
        """The catch-up is the only record that a directory holds files no
        event will ever announce. Spending it on an attempt that failed would
        leave the daemon running the pre-move schedule forever."""
        service = _make_service(tmp_path, existing_cron_dir=False)
        missing = tmp_path / "gone" / "cron"
        _point_at(service, missing)
        attempts = []

        async def flaky_reload():
            attempts.append(1)
            if len(attempts) <= 2:
                raise ValueError("gate plugin no longer imports")
            return _RELOADED

        async def fake_wait(event, seconds):
            missing.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(service, "reload", flaky_reload)
        monkeypatch.setattr(cron_service, "_wait_or_stop", fake_wait)
        # Four idle ticks: nothing here is a change, so every reload attempt
        # has to come from the outstanding catch-up.
        monkeypatch.setattr(
            watchfiles, "awatch", _fake_awatch(set(), set(), set(), set()),
        )
        await service.watch_config()
        # Retried until one succeeded, then stopped retrying.
        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_repeated_reload_failure_is_logged_once(
        self, tmp_path, monkeypatch, caplog
    ):
        """Retrying a catch-up every tick must not turn into a log flood."""
        service = _make_service(tmp_path, existing_cron_dir=False)
        missing = tmp_path / "gone" / "cron"
        _point_at(service, missing)

        attempts = []

        async def always_fails():
            attempts.append(1)
            raise ValueError("gate plugin no longer imports")

        async def fake_wait(event, seconds):
            missing.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(service, "reload", always_fails)
        monkeypatch.setattr(cron_service, "_wait_or_stop", fake_wait)
        monkeypatch.setattr(
            watchfiles, "awatch", _fake_awatch(set(), set(), set()),
        )
        with caplog.at_level(logging.WARNING, logger="nerve.cron.service"):
            await service.watch_config()
        assert len(attempts) == 3  # it really did keep retrying
        assert sum("skipped a bad change" in r.message for r in caplog.records) == 1

    @pytest.mark.asyncio
    async def test_watcher_error_is_retried_after_the_backoff(
        self, tmp_path, monkeypatch
    ):
        """A transient failure to start the watch (inotify limits, a dir that
        vanished) must not cost auto-reload for the life of the process."""
        service = _make_service(tmp_path)
        attempts: list[tuple[str, ...]] = []
        waits = []

        async def flaky_awatch(*dirs, stop_event=None, **kwargs):
            attempts.append(dirs)
            if len(attempts) == 1:
                raise OSError("inotify instance limit reached")
            stop_event.set()
            return
            yield  # pragma: no cover — keeps this an async generator

        async def fake_wait(event, seconds):
            waits.append(seconds)

        monkeypatch.setattr(watchfiles, "awatch", flaky_awatch)
        monkeypatch.setattr(cron_service, "_wait_or_stop", fake_wait)
        await service.watch_config()
        assert len(attempts) == 2
        assert waits == [cron_service._WATCH_RETRY_SECONDS]  # backed off first

    @pytest.mark.asyncio
    async def test_setup_error_is_swallowed(self, tmp_path, monkeypatch):
        """A failure constructing/iterating awatch must degrade to retries, not
        an unhandled task exception."""
        service = _make_service(tmp_path)
        stop = asyncio.Event()
        attempts = []

        def boom(*a, **k):
            attempts.append(1)
            if len(attempts) == 3:
                stop.set()
            raise OSError("inotify instance limit reached")

        monkeypatch.setattr(watchfiles, "awatch", boom)
        monkeypatch.setattr(cron_service, "_wait_or_stop", AsyncMock())
        await service.watch_config(stop_event=stop)  # should not raise
        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_the_same_watcher_error_is_reported_again_after_recovery(
        self, tmp_path, monkeypatch, caplog
    ):
        """Deduping a persistent failure must not silence the second occurrence
        of a transient one — that is the case the dedupe exists for.

        A watch that runs for days never exits its loop, so "it worked again"
        has to be recorded when the watch yields, not when it finishes.
        """
        service = _make_service(tmp_path)
        monkeypatch.setattr(service, "reload", AsyncMock(return_value=_RELOADED))
        monkeypatch.setattr(cron_service, "_wait_or_stop", AsyncMock())
        attempts = []

        async def flaky_awatch(*dirs, stop_event=None, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("inotify instance limit reached")
            if len(attempts) == 2:
                yield set()  # live and healthy for a while...
                raise OSError("inotify instance limit reached")  # ...then dies
            stop_event.set()
            return
            yield  # pragma: no cover — keeps this an async generator

        monkeypatch.setattr(watchfiles, "awatch", flaky_awatch)
        with caplog.at_level(logging.WARNING, logger="nerve.cron.service"):
            await service.watch_config()
        assert sum("watcher failed" in r.message for r in caplog.records) == 2

    @pytest.mark.asyncio
    async def test_stop_event_stops_the_loop(self, tmp_path, monkeypatch):
        service = _make_service(tmp_path)
        monkeypatch.setattr(service, "reload", AsyncMock(return_value=_RELOADED))
        entered = asyncio.Event()

        async def awatch_until_stopped(*dirs, stop_event=None, **kwargs):
            # Faithful-ish async generator: run until stop_event is set, like
            # real awatch honoring its stop_event.
            entered.set()
            while not stop_event.is_set():
                await asyncio.sleep(0.01)
            return
            yield  # noqa — makes this an async generator (unreachable)

        monkeypatch.setattr(watchfiles, "awatch", awatch_until_stopped)
        stop = asyncio.Event()
        task = asyncio.create_task(service.watch_config(stop_event=stop))
        await asyncio.wait_for(entered.wait(), timeout=2)
        stop.set()
        await asyncio.wait_for(task, timeout=2)  # completes without hanging

    @pytest.mark.asyncio
    async def test_cancellation_stops_the_loop(self, tmp_path, monkeypatch):
        service = _make_service(tmp_path)
        entered = asyncio.Event()

        async def never_ending(*dirs, stop_event=None, **kwargs):
            entered.set()
            while True:
                await asyncio.sleep(0.01)
                yield {("modified", "x")}

        monkeypatch.setattr(service, "reload", AsyncMock(return_value=_RELOADED))
        monkeypatch.setattr(watchfiles, "awatch", never_ending)
        task = asyncio.create_task(service.watch_config())
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestShutdownStopsTheWatcher:
    """How the gateway takes the watcher down at shutdown.

    The stop event is the mechanism and cancellation is the backstop, not the
    other way around: a watcher cancelled where it stands can be halfway
    through a reload, leaving the scheduler holding part of the old job set and
    part of the new.
    """

    @pytest.mark.asyncio
    async def test_the_stop_event_alone_ends_the_task(self, tmp_path, monkeypatch):
        """No cancellation involved anywhere: signalling has to be enough on
        its own. If it isn't, the event is decorative and shutdown is really
        just killing the watcher wherever it stands."""
        service = _make_service(tmp_path)
        monkeypatch.setattr(service, "reload", AsyncMock(return_value=_RELOADED))
        entered = asyncio.Event()

        async def awatch_honoring_stop(*dirs, stop_event=None, **kwargs):
            entered.set()
            while not stop_event.is_set():
                await asyncio.sleep(0.01)
            return
            yield  # pragma: no cover — keeps this an async generator

        monkeypatch.setattr(watchfiles, "awatch", awatch_honoring_stop)
        stop = asyncio.Event()
        task = asyncio.create_task(service.watch_config(stop_event=stop))
        await asyncio.wait_for(entered.wait(), timeout=2)

        await stop_background_task(task, stop, "Cron watcher")
        assert task.done()
        assert not task.cancelled()  # it left through its own exit

    @pytest.mark.asyncio
    async def test_a_reload_in_flight_is_allowed_to_finish(
        self, tmp_path, monkeypatch
    ):
        """The reason the stop event has to come first. Shutdown lands while a
        reload is mid-flight; cancelling there abandons the scheduler
        half-rebuilt, so the cycle gets to complete."""
        service = _make_service(tmp_path)
        started, finished = asyncio.Event(), []

        async def slow_reload():
            started.set()
            await asyncio.sleep(0.2)  # stands in for the DB + scheduler work
            finished.append(1)
            return _RELOADED

        monkeypatch.setattr(service, "reload", slow_reload)

        async def awatch_one_change(*dirs, stop_event=None, **kwargs):
            yield {("modified", "jobs.yaml")}
            while not stop_event.is_set():
                await asyncio.sleep(0.01)

        monkeypatch.setattr(watchfiles, "awatch", awatch_one_change)
        stop = asyncio.Event()
        task = asyncio.create_task(service.watch_config(stop_event=stop))
        await asyncio.wait_for(started.wait(), timeout=2)

        await stop_background_task(task, stop, "Cron watcher")
        assert finished == [1]
        assert not task.cancelled()

    @pytest.mark.asyncio
    async def test_a_task_that_will_not_stop_is_cancelled_after_the_timeout(
        self, caplog
    ):
        """The backstop still exists: a wedged task must not hold shutdown open
        indefinitely, and giving up on one should say so out loud."""
        async def deaf_to_the_event():
            while True:
                await asyncio.sleep(0.01)

        task = asyncio.create_task(deaf_to_the_event())
        with caplog.at_level(logging.WARNING, logger="nerve.utils.aio"):
            await stop_background_task(
                task, asyncio.Event(), "Cron watcher", timeout=0.2
            )
        assert task.cancelled()
        assert "did not stop within" in caplog.text

    @pytest.mark.asyncio
    async def test_without_a_stop_event_cancellation_is_the_only_lever(self):
        """Setup can fail after the task exists but before the event does."""
        async def forever():
            while True:
                await asyncio.sleep(0.01)

        task = asyncio.create_task(forever())
        await stop_background_task(task, None, "Cron watcher")
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_a_task_that_already_died_is_logged_not_raised(self, caplog):
        """Shutdown continues past a background task that crashed earlier —
        there is a whole teardown sequence after this call."""
        async def boom():
            raise RuntimeError("inotify watch descriptor gone")

        task = asyncio.create_task(boom())
        with caplog.at_level(logging.WARNING, logger="nerve.utils.aio"):
            await stop_background_task(
                task, asyncio.Event(), "Cron watcher"
            )
        assert "inotify watch descriptor gone" in caplog.text


@pytest.mark.slow
class TestWatchConfigForReal:
    """Drives real watchfiles against a real directory.

    The mocked tests above pin the control flow, but they cannot see what the
    filesystem actually reports — which is where the interesting bugs are: a
    directory's mtime bump is a change event of its own, and files already
    present when a watch starts produce no event at all.
    """

    @pytest.mark.asyncio
    async def test_watch_follows_the_config_and_one_edit_means_one_reload(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(cron_service, "_WATCH_RETRY_SECONDS", 0.5)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cron = config_dir / "cron"
        moved = tmp_path / "moved"
        service = _make_service(tmp_path, existing_cron_dir=False)
        _point_at(service, cron)

        reloads = []

        async def reload_importing_plugins():
            # The real reload imports every gate plugin, and importing writes
            # bytecode into the directory being watched.
            from nerve.cron.gate_plugins import load_gate_plugins

            load_gate_plugins(service.config.cron.gate_plugins_dir, replace=True)
            reloads.append(1)
            return _RELOADED

        monkeypatch.setattr(service, "reload", reload_importing_plugins)

        # Record what each watch is actually handed, and delegate to the real
        # thing. Watching the recorder — rather than what the paths happen to
        # look like on disk — is what makes the phases below synchronizable.
        real_awatch = watchfiles.awatch
        entered: list[tuple[str, ...]] = []

        def recording_awatch(*dirs, **kwargs):
            entered.append(dirs)
            return real_awatch(*dirs, **kwargs)

        monkeypatch.setattr(watchfiles, "awatch", recording_awatch)

        stop = asyncio.Event()
        task = asyncio.create_task(service.watch_config(stop_event=stop))
        try:
            # Phase 1: the cron dir is missing, so its parent stands in.
            await _until(lambda: entered == [(str(config_dir),)])
            (config_dir / "settings.yaml").write_text("unrelated: 1\n")
            await asyncio.sleep(3)
            assert reloads == []  # a sibling in the parent is not cron config

            # Phase 2: the cron dir appears, fully populated.
            (cron / "gates").mkdir(parents=True)
            (cron / "gates" / "probe.py").write_text("VALUE = 1\n")
            (cron / "jobs.yaml").write_text("jobs: []\n")
            await _until(lambda: len(entered) >= 2, timeout=25)
            assert entered[1] == (str(cron), str(cron / "gates"))  # narrowed
            await _until(lambda: len(reloads) >= 1, timeout=25)

            # Phase 3: quiet, then exactly one edit.
            await asyncio.sleep(4)
            before = len(reloads)
            (cron / "jobs.yaml").write_text("jobs: []\n# edited\n")
            await _until(lambda: len(reloads) > before, timeout=25)
            assert (cron / "gates" / "__pycache__").is_dir()  # probe is valid
            await asyncio.sleep(6)  # long enough for a re-trigger to land
            assert len(reloads) - before == 1

            # Phase 4: repointing the config moves the watch. A watcher bound
            # to its directories once keeps watching a location reload() no
            # longer reads, and auto-reload silently stops firing.
            (moved / "gates").mkdir(parents=True)
            _point_at(service, moved)
            await _until(lambda: len(entered) >= 3, timeout=25)
            assert entered[2] == (str(moved), str(moved / "gates"))
            before = len(reloads)
            (moved / "jobs.yaml").write_text("jobs: []\n# at the new location\n")
            await _until(lambda: len(reloads) > before, timeout=25)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=10)


class TestAutoReloadFlag:
    def test_default_enabled(self):
        assert CronConfig.from_dict({}).auto_reload is True

    def test_can_disable(self):
        assert CronConfig.from_dict({"auto_reload": False}).auto_reload is False

    @pytest.mark.parametrize("text", ["false", "False", "0", "off", "no"])
    def test_string_spellings_of_false_disable_it(self, text):
        """A quoted scalar — or any ``${VAR}`` reference, which interpolation
        always turns into a string — must still switch the watcher off."""
        assert CronConfig.from_dict({"auto_reload": text}).auto_reload is False

    def test_env_reference_can_disable_the_watcher(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NERVE_CRON_AUTO_RELOAD", "false")
        (tmp_path / "config.yaml").write_text(
            'cron:\n  auto_reload: "${NERVE_CRON_AUTO_RELOAD}"\n', encoding="utf-8"
        )
        assert load_config(tmp_path).cron.auto_reload is False

"""Tests for nerve.paths — the central machine-local directory resolver."""

import ast
import os
import re
from pathlib import Path

from nerve import paths


class TestNerveHome:
    def test_default_is_dot_nerve_under_home(self, monkeypatch):
        # The autouse conftest fixture sets NERVE_HOME; clear it to see the default.
        monkeypatch.delenv(paths.NERVE_HOME_ENV, raising=False)
        assert paths.nerve_home() == Path.home() / ".nerve"

    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv(paths.NERVE_HOME_ENV, str(tmp_path / "state"))
        assert paths.nerve_home() == tmp_path / "state"

    def test_env_override_expands_user(self, monkeypatch):
        monkeypatch.setenv(paths.NERVE_HOME_ENV, "~/custom-nerve")
        assert paths.nerve_home() == Path.home() / "custom-nerve"

    def test_blank_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(paths.NERVE_HOME_ENV, "   ")
        assert paths.nerve_home() == Path.home() / ".nerve"

    def test_override_is_read_lazily(self, monkeypatch, tmp_path):
        """Each call re-reads the env var — no value frozen at import."""
        monkeypatch.setenv(paths.NERVE_HOME_ENV, str(tmp_path / "a"))
        assert paths.nerve_home() == tmp_path / "a"
        monkeypatch.setenv(paths.NERVE_HOME_ENV, str(tmp_path / "b"))
        assert paths.nerve_home() == tmp_path / "b"

    def test_relative_override_is_made_absolute(self, monkeypatch, tmp_path):
        """A relative override must not be handed to subprocesses verbatim."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(paths.NERVE_HOME_ENV, "relative/state")
        home = paths.nerve_home()
        assert home.is_absolute()
        assert home == Path(os.getcwd()) / "relative" / "state"

    def test_override_is_normalized(self, monkeypatch, tmp_path):
        monkeypatch.setenv(paths.NERVE_HOME_ENV, f"{tmp_path}/a/../b/./c")
        assert paths.nerve_home() == tmp_path / "b" / "c"

    def test_absolute_override_is_normalized_too(self, monkeypatch, tmp_path):
        """One directory, one spelling — however the operator wrote it.

        Two processes that compare state paths as strings must agree on
        whether they share a state dir, so trailing/duplicate slashes and
        ``.``/``..`` segments are collapsed rather than carried around.
        """
        for spelling in (f"{tmp_path}/state/", f"{tmp_path}//state", f"{tmp_path}/./state"):
            monkeypatch.setenv(paths.NERVE_HOME_ENV, spelling)
            assert paths.nerve_home() == tmp_path / "state", spelling

    def test_dotdot_is_collapsed_lexically_not_through_symlinks(self, monkeypatch, tmp_path):
        """``..`` is resolved textually, and symlinks are left alone.

        Documented deliberately: normalization is ``abspath``, not
        ``realpath``, so callers must not expect a canonical path back.
        """
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "elsewhere")
        monkeypatch.setenv(paths.NERVE_HOME_ENV, f"{tmp_path}/link/../state")
        assert paths.nerve_home() == tmp_path / "state"

        # ...and a symlink that is not followed by ``..`` survives untouched.
        monkeypatch.setenv(paths.NERVE_HOME_ENV, f"{tmp_path}/link/state")
        assert paths.nerve_home() == tmp_path / "link" / "state"

    def test_accessors_are_absolute_under_relative_override(self, monkeypatch, tmp_path):
        """Every derived path inherits the absolute base, not just nerve_home()."""
        # os.getcwd() is canonicalized, so a relative override is anchored to
        # the real path of the cwd — compare against that, not tmp_path.
        monkeypatch.chdir(tmp_path)
        real = Path(os.getcwd())
        monkeypatch.setenv(paths.NERVE_HOME_ENV, "state")
        for path in (paths.db_path(), paths.pid_file(), paths.log_file(),
                     paths.cron_dir(), paths.config_pointer_file()):
            assert path.is_absolute(), path
            assert path.parent == real / "state", path


class TestAccessors:
    def test_accessors_live_under_nerve_home(self, monkeypatch, tmp_path):
        home = tmp_path / "state"
        monkeypatch.setenv(paths.NERVE_HOME_ENV, str(home))
        assert paths.nerve_path("x", "y") == home / "x" / "y"
        assert paths.config_pointer_file() == home / "config_dir"
        assert paths.db_path() == home / "nerve.db"
        assert paths.pid_file() == home / "nerve.pid"
        assert paths.log_file() == home / "nerve.log"
        assert paths.cache_dir() == home / "cache"
        assert paths.memu_sqlite() == home / "memu.sqlite"
        assert paths.cron_dir() == home / "cron"

    def test_default_workspace_is_not_under_state_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv(paths.NERVE_HOME_ENV, str(tmp_path / "state"))
        ws = paths.default_workspace()
        assert ws == Path.home() / "nerve-workspace"
        assert (tmp_path / "state") not in ws.parents


class TestNothingBypassesTheProvider:
    """A provider only helps if nothing builds the same paths without it.

    ``NERVE_HOME`` is supposed to move every machine-local file at once. Code
    that names a literal home instead keeps writing to the default, so one
    instance reads the state another wrote — and nothing fails loudly, because
    both paths exist and both are writable. Each such site has to be found by
    reading the diff, which is exactly how the last few arrived.

    Both spellings of the mistake are equally invisible: ``Path("~/.nerve/x")``
    and ``Path.home() / ".nerve" / "x"`` ignore the override identically, so
    the guard has to know about both.
    """

    # Deliberately narrow: these match *constructing* a path from a literal
    # home, not naming one. Comments and docstrings that mention the default
    # location are normal and there are dozens of them — every pattern here
    # needs a call and a quoted path component, which prose never has.
    _BUILDS_FROM_LITERAL_HOME = re.compile(
        r"""
          (?: Path | expanduser ) \( \s* ["'] ~/\.nerve   # Path("~/.nerve/x")
        | \.home\(\) \s* (?: / | \.joinpath\( ) \s* ["']\.nerve   # Path.home() / ".nerve"
        """,
        re.VERBOSE,
    )

    def test_no_module_builds_a_machine_local_path_from_a_literal_home(self):
        package_root = Path(paths.__file__).parent
        offenders: list[str] = []
        for source in sorted(package_root.rglob("*.py")):
            # paths.py is the provider itself; its module docstring names the
            # very pattern it exists to replace.
            if source == Path(paths.__file__):
                continue
            for lineno, line in enumerate(
                source.read_text(encoding="utf-8").splitlines(), start=1,
            ):
                if self._BUILDS_FROM_LITERAL_HOME.search(line):
                    rel = source.relative_to(package_root.parent)
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")

        assert not offenders, (
            "Build machine-local paths through nerve.paths so NERVE_HOME is "
            "honored (nerve_path/db_path/cron_dir/...), instead of naming a "
            "literal home:\n" + "\n".join(offenders)
        )

    def test_the_guard_catches_both_spellings(self):
        """The guard is the only thing standing between us and a silent bypass.

        A typo in the regex would make it pass forever, so pin what it does and
        does not match: constructions are offences, prose about the default
        location is not.
        """
        offences = [
            'CONFIG = Path("~/.nerve/config_dir")',
            "cache = Path('~/.nerve') / 'cache'",
            'db = os.path.expanduser("~/.nerve/nerve.db")',
            'state = Path.home() / ".nerve" / "cache"',
            "state = Path.home()/'.nerve'",
            'state = Path.home().joinpath(".nerve", "cache")',
        ]
        allowed = [
            "# Defaults to ~/.nerve/nerve.db unless NERVE_HOME says otherwise.",
            '"""The daemon PID file (``~/.nerve/nerve.pid``)."""',
            'claude_dir = Path.home() / ".claude"',
            'ws = Path.home() / "nerve-workspace"',
            'db = paths.nerve_path("nerve.db")',
        ]
        for line in offences:
            assert self._BUILDS_FROM_LITERAL_HOME.search(line), line
        for line in allowed:
            assert not self._BUILDS_FROM_LITERAL_HOME.search(line), line


class TestImportTimeConstantsRespectTheOverride:
    """Lazy accessors are not enough when a module freezes one into a constant.

    ``nerve_home()`` re-reads ``NERVE_HOME`` on every call, but a module-level
    ``X = paths.nerve_path(...)`` is evaluated once, when the module is
    imported. Under pytest that happens during collection — before any fixture
    runs — so such a constant names the *real* ``~/.nerve`` for the whole
    session, and ``from nerve.config import X`` copies that stale Path into
    every importer's namespace. A test that forgets to patch it then reads and
    writes the developer's live install; the resume drainer even unlinks its
    file. The autouse fixture in conftest re-points every copy, and these tests
    keep it honest.
    """

    def test_known_constants_point_into_the_isolated_state_dir(self):
        from nerve import cli, config
        from nerve.agent import engine

        expected = paths.nerve_path("resume-after-restart")
        for module in (config, cli, engine):
            assert module.RESUME_QUEUE_FILE == expected, module.__name__

    def test_doctor_config_source_label_follows_the_override(self, monkeypatch, tmp_path):
        """A path baked into a *string* is just as frozen as one in a Path.

        The doctor tells the reader which pointer file the config came from;
        resolved at import time it can name a file this process never read.
        """
        from nerve import cli

        monkeypatch.setenv(paths.NERVE_HOME_ENV, str(tmp_path / "elsewhere"))
        label = cli._config_source_label("pointer")
        assert str(tmp_path / "elsewhere" / "config_dir") in label

    def test_no_unhandled_module_level_state_path_exists(self):
        """A new import-time constant has to be added to the conftest fixture.

        Without this the next one is isolated nowhere and nothing says so: the
        suite keeps passing while quietly writing outside the temp state dir.
        """
        # Read the inventory the fixture actually uses, so adding an entry
        # there is all it takes — no second list to forget.
        from tests.conftest import _IMPORT_TIME_STATE_PATHS

        package_root = Path(paths.__file__).parent
        found: list[str] = []
        for source in sorted(package_root.rglob("*.py")):
            if source == Path(paths.__file__):
                continue  # the provider's own accessors are functions, not constants
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in tree.body:  # module scope only
                targets = (
                    node.targets if isinstance(node, ast.Assign)
                    else [node.target] if isinstance(node, ast.AnnAssign) and node.value
                    else []
                )
                if not targets or "paths." not in ast.unparse(node.value):
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in _IMPORT_TIME_STATE_PATHS:
                        rel = source.relative_to(package_root.parent)
                        found.append(f"{rel}:{node.lineno}: {target.id}")

        assert not found, (
            "These module-level constants materialize a machine-local path at "
            "import time, which is before the test fixtures can move the state "
            "dir. Add each to _IMPORT_TIME_STATE_PATHS in tests/conftest.py (or "
            "make it a function so it resolves lazily):\n" + "\n".join(found)
        )

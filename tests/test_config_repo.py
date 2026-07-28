"""Tests for config-repo scaffolding (nerve/config_repo.py + `config init-repo`)."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import nerve
from nerve import config_repo
from nerve.config_repo import _SCAFFOLD, _template_dir, scaffold_config_repo

_WORKFLOW = ".github/workflows/validate-config.yml"
#: The stand-in revision docs/config.md shows in its copy of the workflow.
_DOCS_REF = "<the nerve revision your instance runs>"


def _fake_ref(monkeypatch, ref: str, note: str, pinned: bool) -> None:
    """Pin what `_validator_ref` reports, so a test isn't at the mercy of the
    git state of the checkout it happens to be running in."""
    monkeypatch.setattr(config_repo, "_validator_ref", lambda: (ref, note, pinned))


#: Classes that exercise `_validator_ref` itself. Everything else gets the stub
#: below: resolving the pin shells out to git three times, which costs seconds on
#: a network filesystem, and most tests here only need *a* workflow file.
_REAL_REF_CLASSES = {"TestPinHonesty", "TestValidatorRefProbe"}


@pytest.fixture(autouse=True)
def _stub_validator_ref(request, monkeypatch):
    if request.cls is not None and request.cls.__name__ in _REAL_REF_CLASSES:
        return
    _fake_ref(monkeypatch, _DOCS_REF, config_repo._PINNED_NOTE, True)


def _workflow(ws: Path) -> dict:
    return yaml.safe_load((ws / _WORKFLOW).read_text(encoding="utf-8"))


def _steps(ws: Path) -> list[dict]:
    return _workflow(ws)["jobs"]["validate"]["steps"]


def _validate_step(ws: Path) -> dict:
    """The step that actually runs the validator."""
    return next(
        s for s in _steps(ws) if "nerve.config_validate" in str(s.get("run", ""))
    )


def _nerve_checkout(ws: Path) -> dict:
    """The step that checks nerve out to validate with."""
    return next(
        s for s in _steps(ws)
        if (s.get("with") or {}).get("repository") == "ClickHouse/nerve"
    )


def _run_ci_validation(ws: Path) -> subprocess.CompletedProcess:
    """Run the scaffolded workflow's validate step against ``ws``, as CI would.

    The command, its environment and its working directory are taken from the
    generated file rather than restated here, so this exercises what an
    operator's CI will actually execute — flags included. The only substitution
    is the nerve source: CI clones it into ``.nerve-src``, here it is the tree
    the tests are running from.
    """
    step = _validate_step(ws)
    argv = shlex.split(step["run"])
    assert argv[0] == "python", step["run"]
    argv[0] = sys.executable

    source_root = str(Path(nerve.__file__).resolve().parent.parent)
    step_env = step.get("env") or {}
    # Fail loudly rather than run a test that proves nothing: if the template
    # stopped putting nerve on the path this way, the substitution below would
    # quietly no-op and the subprocess would import an installed nerve instead
    # of the tree under test — including in the test that checks the config repo
    # itself can't be imported.
    assert ".nerve-src" in str(step_env.get("PYTHONPATH", "")), step_env
    env = dict(os.environ)
    env.update({
        k: str(v).replace(".nerve-src", source_root) for k, v in step_env.items()
    })
    return subprocess.run(argv, cwd=ws, env=env, capture_output=True, text=True)


class TestScaffold:
    def test_creates_all_files(self, tmp_path):
        result = scaffold_config_repo(tmp_path)
        assert set(result.created) == set(_SCAFFOLD)
        assert result.skipped == []
        for rel in _SCAFFOLD:
            assert (tmp_path / rel).is_file()

    def test_ci_workflow_lands_in_github_dir(self, tmp_path):
        scaffold_config_repo(tmp_path)
        wf = tmp_path / ".github/workflows/validate-config.yml"
        assert wf.is_file()
        assert "--workspace ." in wf.read_text(encoding="utf-8")

    def test_ci_workflow_is_valid_yaml(self, tmp_path):
        # A malformed template would ship and only blow up in the operator's CI.
        scaffold_config_repo(tmp_path)
        wf = yaml.safe_load(
            (tmp_path / ".github/workflows/validate-config.yml").read_text("utf-8")
        )
        steps = wf["jobs"]["validate"]["steps"]
        assert any("nerve.config_validate" in str(s.get("run", "")) for s in steps)

    def test_ci_workflow_needs_no_install_or_secrets(self, tmp_path):
        # The validator runs from a source checkout: no `pip install nerve`,
        # and nerve is public so no token/secret is required. Assert against the
        # parsed steps so prose in comments can't satisfy (or break) this.
        scaffold_config_repo(tmp_path)
        wf = yaml.safe_load(
            (tmp_path / ".github/workflows/validate-config.yml").read_text("utf-8")
        )
        steps = wf["jobs"]["validate"]["steps"]
        runs = " ".join(str(s.get("run", "")) for s in steps)
        assert "install nerve" not in runs.lower()
        assert "nerve @" not in runs  # no VCS/package install of nerve itself
        # nerve is checked out and run in place instead.
        assert any(
            (s.get("with") or {}).get("repository") == "ClickHouse/nerve"
            for s in steps
        )
        # No Actions secret anywhere in the workflow.
        assert "secrets." not in (
            tmp_path / ".github/workflows/validate-config.yml"
        ).read_text("utf-8")

    def test_gitignore_excludes_secrets(self, tmp_path):
        scaffold_config_repo(tmp_path)
        body = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert "config.local.yaml" in body
        assert "*.migrated" in body

    def test_gitignore_covers_backup_secret_members(self, tmp_path):
        # If the workspace doubles as the state dir, `git add -A` (which the
        # runbook tells operators to run) must not sweep up nerve's credentials.
        from nerve.backup import SECRET_MEMBERS

        scaffold_config_repo(tmp_path)
        patterns = {
            ln.strip()
            for ln in (tmp_path / ".gitignore").read_text("utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        }
        for member in SECRET_MEMBERS:
            name = Path(member).name
            assert any(
                p in (name, f"{name}/", f"*{Path(name).suffix}")
                for p in patterns
            ), f"{name} (from backup.SECRET_MEMBERS) is not gitignored"

    def test_dry_run_writes_nothing(self, tmp_path):
        result = scaffold_config_repo(tmp_path, dry_run=True)
        assert set(result.created) == set(_SCAFFOLD)
        for rel in _SCAFFOLD:
            assert not (tmp_path / rel).exists()
        # Not even the parent dirs of nested targets.
        assert not (tmp_path / ".github").exists()
        assert list(tmp_path.iterdir()) == []

    def test_templates_are_packaged(self):
        # Guards the importlib-vs-source fallback in _template_dir(): every
        # scaffold source must actually resolve, or an installed nerve breaks.
        tmpl = _template_dir()
        for src_name in _SCAFFOLD.values():
            assert (tmpl / src_name).is_file(), f"missing template {src_name}"

    def test_idempotent_skips_existing(self, tmp_path):
        scaffold_config_repo(tmp_path)
        second = scaffold_config_repo(tmp_path)
        assert second.created == []
        assert set(second.skipped) == set(_SCAFFOLD)

    def test_never_overwrites_existing_file(self, tmp_path):
        gi = tmp_path / ".gitignore"
        gi.write_text("# my custom ignore\n", encoding="utf-8")
        result = scaffold_config_repo(tmp_path)
        assert ".gitignore" in result.skipped
        assert gi.read_text(encoding="utf-8") == "# my custom ignore\n"
        # The other two are still created.
        assert (tmp_path / "README.md").is_file()

    def test_detects_existing_git_repo(self, tmp_path):
        assert scaffold_config_repo(tmp_path).is_git_repo is False
        (tmp_path / ".git").mkdir()
        assert scaffold_config_repo(tmp_path).is_git_repo is True

    def test_seeds_a_portable_settings_layer(self, tmp_path):
        # The workflow validates the portable layer only, and a config/ it can
        # read nothing out of is an error there — so a repo scaffolded from a
        # bare directory needs one to be valid on its first commit.
        scaffold_config_repo(tmp_path)
        assert (tmp_path / "config" / "settings.yaml").is_file()

    def test_leaves_an_existing_settings_file_alone(self, tmp_path):
        settings = tmp_path / "config" / "settings.yaml"
        settings.parent.mkdir(parents=True)
        settings.write_text("timezone: UTC\n", encoding="utf-8")

        result = scaffold_config_repo(tmp_path)

        assert "config/settings.yaml" in result.skipped
        assert settings.read_text(encoding="utf-8") == "timezone: UTC\n"


class TestScaffoldedWorkflow:
    """What the generated workflow does, read off the generated file."""

    def test_validator_revision_is_pinned(self, tmp_path, monkeypatch):
        # --strict-keys is only meaningful against the nerve the instance runs:
        # an older validator rejects keys a newer instance understands.
        _fake_ref(monkeypatch, "b" * 40, config_repo._PINNED_NOTE, True)
        result = scaffold_config_repo(tmp_path)

        assert _nerve_checkout(tmp_path)["with"]["ref"] == "b" * 40
        assert (result.validator_ref, result.validator_pinned) == ("b" * 40, True)
        assert "is pinned" in (tmp_path / _WORKFLOW).read_text("utf-8")

    def test_validates_the_portable_layer_strictly(self, tmp_path):
        scaffold_config_repo(tmp_path)
        run = _validate_step(tmp_path)["run"]
        assert "--portable-only" in run  # not the runner's machine config
        assert "--strict-keys" in run  # a typo'd key blocks the PR

    def test_does_not_assume_lockdown(self, tmp_path):
        # Correct default, and load-bearing: the locked view requires secrets a
        # fresh repo hasn't got, so passing it here would red-X every new repo.
        # The workflow comment points at it for repos that do serve a locked box.
        scaffold_config_repo(tmp_path)
        assert "--assume-lockdown" not in _validate_step(tmp_path)["run"]
        assert "--assume-lockdown" in (tmp_path / _WORKFLOW).read_text("utf-8")

    def test_scans_for_committed_secrets(self, tmp_path):
        scaffold_config_repo(tmp_path)
        assert any("gitleaks" in str(s.get("uses", "")) for s in _steps(tmp_path))

    def test_no_step_runs_code_from_the_config_repo(self, tmp_path):
        """The job reads the PR's files; it must never execute them.

        Validation itself declines to load the bundle's gate plugins, which is
        worth nothing if the job around it installs the repo, runs a script out
        of it, or resolves a local action from it.
        """
        scaffold_config_repo(tmp_path)
        for step in _steps(tmp_path):
            uses = str(step.get("uses", ""))
            assert not uses.startswith("./"), f"local action from the PR: {uses}"
            # A container step can point its entrypoint at the mounted checkout,
            # which `uses:` alone doesn't reveal.
            entrypoint = str((step.get("with") or {}).get("entrypoint", ""))
            assert "/github/workspace" not in entrypoint, entrypoint
            run = str(step.get("run", ""))
            for forbidden in (
                "-e .", "-r ", "install .", "bash ", "sh ", "make ",
                "pre-commit", "setup.py", "uv run", "npm ", "npx ", "tox",
            ):
                assert forbidden not in run, f"{forbidden!r} in step: {run}"

    def test_docs_show_the_generated_workflow_verbatim(self, tmp_path, monkeypatch):
        """docs/config.md prints this file inline, comments and all.

        Compared as text, not as parsed YAML: every load-bearing instruction in
        that file — which flags matter, what the pin is for, what silences a
        false positive — lives in a comment, so a structural comparison would
        wave through exactly the drift worth catching.
        """
        _fake_ref(monkeypatch, _DOCS_REF, config_repo._PINNED_NOTE, True)
        scaffold_config_repo(tmp_path)
        generated = (tmp_path / _WORKFLOW).read_text(encoding="utf-8").rstrip("\n")

        docs = Path(__file__).resolve().parent.parent / "docs" / "config.md"
        text = docs.read_text(encoding="utf-8")
        start = text.index("```yaml\nname: validate-config\n") + len("```yaml\n")
        documented = text[start:text.index("\n```", start)]

        assert documented == generated


class TestPinHonesty:
    """The generated file may not claim a pin it hasn't got.

    An unpinned workflow that says "pinned" is the version skew the pin exists to
    prevent, arriving months later with no PR to blame it on: `main` moves, a key
    is renamed upstream, and --strict-keys red-Xes a config repo that is fine.
    """

    def _scaffold_with_git_source(self, tmp_path, monkeypatch, responses: dict):
        """Scaffold against a stand-in nerve checkout whose git answers are
        ``responses``, keyed by subcommand."""
        source = tmp_path / "nerve-src"
        (source / ".git").mkdir(parents=True)
        monkeypatch.setattr(config_repo, "_source_root", lambda: source)
        monkeypatch.setattr(
            config_repo, "_git", lambda args, cwd: responses.get(args[0]),
        )
        ws = tmp_path / "repo"
        ws.mkdir()
        return ws, scaffold_config_repo(ws)

    def test_a_reachable_clean_commit_is_pinned(self, tmp_path, monkeypatch):
        ws, result = self._scaffold_with_git_source(tmp_path, monkeypatch, {
            "rev-parse": "c" * 40, "status": "", "branch": "  origin/main",
        })
        assert (result.validator_ref, result.validator_pinned) == ("c" * 40, True)
        assert _nerve_checkout(ws)["with"]["ref"] == "c" * 40

    def test_a_commit_no_remote_has_is_not_pinned(self, tmp_path, monkeypatch):
        """actions/checkout fails hard on an unfetchable ref, so pinning a local
        commit breaks every PR at that step — and init-repo can't repair it,
        because it never overwrites."""
        ws, result = self._scaffold_with_git_source(tmp_path, monkeypatch, {
            "rev-parse": "d" * 40, "status": "", "branch": "",
        })
        assert result.validator_pinned is False
        assert _nerve_checkout(ws)["with"]["ref"] == "main"
        body = (ws / _WORKFLOW).read_text("utf-8")
        assert "is NOT pinned" in body
        assert "no remote branch" in body

    def test_a_dirty_checkout_is_not_pinned(self, tmp_path, monkeypatch):
        # The SHA doesn't describe what this instance is running.
        ws, result = self._scaffold_with_git_source(tmp_path, monkeypatch, {
            "rev-parse": "e" * 40, "status": " M nerve/config.py", "branch": "origin/main",
        })
        assert result.validator_pinned is False
        assert "uncommitted changes" in (ws / _WORKFLOW).read_text("utf-8")

    def test_a_non_checkout_install_is_not_pinned(self, tmp_path, monkeypatch):
        """Installed from a wheel: no revision to pin to, and the placeholder
        must never reach the operator's CI."""
        monkeypatch.setattr(
            config_repo, "_source_root", lambda: tmp_path / "site-packages/nerve",
        )
        scaffold_config_repo(tmp_path)

        body = (tmp_path / _WORKFLOW).read_text(encoding="utf-8")
        assert "__NERVE_REF__" not in body and "__NERVE_REF_NOTE__" not in body
        assert _nerve_checkout(tmp_path)["with"]["ref"] == "main"
        assert "is NOT pinned" in body

    def test_the_note_is_a_comment_and_the_file_still_parses(
        self, tmp_path, monkeypatch,
    ):
        # A long interpolated reason is wrapped; every line of it has to stay
        # commented or the workflow is unparseable YAML on the operator's CI.
        _fake_ref(monkeypatch, "main", config_repo._UNPINNED_NOTE.format(
            reason="x " * 60, default="main",
        ), False)
        scaffold_config_repo(tmp_path)

        body = (tmp_path / _WORKFLOW).read_text(encoding="utf-8")
        note_lines = [
            ln for ln in body.splitlines() if "NOT pinned" in ln or "x x x" in ln
        ]
        assert note_lines
        assert all(ln.lstrip().startswith("#") for ln in note_lines), note_lines
        assert yaml.safe_load(body)["jobs"]["validate"]["steps"]


class TestValidatorRefProbe:
    """The real probe, run once against the checkout the suite lives in."""

    def test_reports_a_ref_a_note_and_whether_it_is_a_pin(self):
        ref, note, pinned = config_repo._validator_ref()

        assert ref and "__NERVE_REF__" not in note
        # Whatever the answer, the note must agree with it — that agreement is
        # the whole point, and it is decided here rather than in the template.
        assert ("is pinned" in note) is pinned
        assert ("is NOT pinned" in note) is not pinned
        if not pinned:
            assert ref == config_repo._DEFAULT_REF


class TestFreshRepoPassesItsOwnCI:
    """`init-repo` scaffolds the repo *and* the job that gates it, so the two
    have to agree on day one — a red X on the first commit teaches operators to
    ignore the check."""

    def test_a_fresh_scaffold_validates_clean(self, tmp_path):
        ws = tmp_path / "repo"
        ws.mkdir()
        scaffold_config_repo(ws)

        proc = _run_ci_validation(ws)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "Config OK" in proc.stdout

    def test_a_broken_bundle_fails(self, tmp_path):
        ws = tmp_path / "repo"
        ws.mkdir()
        scaffold_config_repo(ws)
        (ws / "config" / "settings.yaml").write_text(
            "agent:\n  backend: bogus\n", encoding="utf-8",
        )

        proc = _run_ci_validation(ws)

        assert proc.returncode == 1
        assert "bogus" in proc.stdout

    def test_a_misspelled_key_blocks_the_pr(self, tmp_path):
        ws = tmp_path / "repo"
        ws.mkdir()
        scaffold_config_repo(ws)
        (ws / "config" / "settings.yaml").write_text(
            "tiimezone: UTC\n", encoding="utf-8",
        )

        proc = _run_ci_validation(ws)

        assert proc.returncode == 1, proc.stdout
        assert "tiimezone" in proc.stdout

    def test_machine_config_beside_the_checkout_is_ignored(self, tmp_path):
        """A config.yaml in the working directory is picked up by the config-dir
        waterfall; merging it would let the runner's state decide the verdict."""
        ws = tmp_path / "repo"
        ws.mkdir()
        scaffold_config_repo(ws)
        (ws / "config" / "settings.yaml").write_text(
            "agent:\n  backend: bogus\n", encoding="utf-8",
        )
        (ws / "config.yaml").write_text("agent:\n  backend: claude\n", encoding="utf-8")

        proc = _run_ci_validation(ws)

        assert proc.returncode == 1, proc.stdout
        assert "bogus" in proc.stdout

    def test_a_machine_local_cron_dir_cannot_fail_the_repo(
        self, tmp_path, monkeypatch,
    ):
        """The repo carries no cron jobs, so cron resolution would otherwise fall
        back to the machine-local directory — condemning a clean config repo over
        a file that isn't in it. Bites hardest where this command is most likely
        to be run by hand: the instance box, which has one."""
        ws = tmp_path / "repo"
        ws.mkdir()
        scaffold_config_repo(ws)
        home = tmp_path / "nervehome"
        (home / "cron").mkdir(parents=True)
        (home / "cron" / "jobs.yaml").write_text("jobs: [oops\n", encoding="utf-8")
        monkeypatch.setenv("NERVE_HOME", str(home))

        proc = _run_ci_validation(ws)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "oops" not in proc.stdout

    def test_a_gate_plugin_neither_runs_nor_fails_the_job(self, tmp_path):
        """--strict-keys must not turn "can't confirm" into "reject".

        A gate type only a plugin provides is unverifiable without importing the
        plugin, which the job declines to do — so it stays a warning, and the
        plugin's code never executes in CI.
        """
        ws = tmp_path / "repo"
        ws.mkdir()
        scaffold_config_repo(ws)
        marker = tmp_path / "executed"
        gates = ws / "config" / "cron" / "gates"
        gates.mkdir(parents=True)
        (gates / "marker.py").write_text(
            f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        (ws / "config" / "cron" / "jobs.yaml").write_text(
            "jobs:\n  - id: g\n    schedule: 1h\n    prompt: hi\n"
            "    run_if:\n      - type: marker_test\n",
            encoding="utf-8",
        )

        proc = _run_ci_validation(ws)

        assert not marker.exists(), "CI executed a gate plugin from the PR"
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "marker_test" in proc.stdout

    def test_the_checkout_is_not_importable(self, tmp_path):
        """`python -m` puts the working directory first on sys.path, so a
        `yaml.py` committed to the config repo would be imported in place of the
        real one — running a pull request's code before anyone read it."""
        ws = tmp_path / "repo"
        ws.mkdir()
        scaffold_config_repo(ws)
        marker = tmp_path / "imported"
        (ws / "yaml.py").write_text(
            f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )

        proc = _run_ci_validation(ws)

        assert not marker.exists(), "the config repo's yaml.py was imported"
        assert proc.returncode == 0, proc.stdout + proc.stderr


class TestInitRepoCommand:
    def _run(self, args):
        from nerve.cli import main

        return CliRunner().invoke(main, args, obj={"config": None, "config_dir": "."})

    def test_scaffolds_via_workspace_flag(self, tmp_path):
        result = self._run(["config", "init-repo", "--workspace", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".github/workflows/validate-config.yml").is_file()
        assert "Created:" in result.output
        assert "Next steps:" in result.output

    def test_prints_git_init_when_not_a_repo(self, tmp_path):
        out = self._run(["config", "init-repo", "--workspace", str(tmp_path)]).output
        assert "git init" in out

    def test_omits_git_init_when_already_a_repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        out = self._run(["config", "init-repo", "--workspace", str(tmp_path)]).output
        assert "git init" not in out

    def test_dry_run_writes_nothing(self, tmp_path):
        out = self._run(
            ["config", "init-repo", "--workspace", str(tmp_path), "--dry-run"]
        ).output
        assert "Would create:" in out
        assert not (tmp_path / ".gitignore").exists()

    def test_reports_a_real_pin(self, tmp_path, monkeypatch):
        _fake_ref(monkeypatch, "f" * 40, config_repo._PINNED_NOTE, True)
        out = self._run(["config", "init-repo", "--workspace", str(tmp_path)]).output
        assert "pinned to ffffffffffff" in out
        assert "NOT be pinned" not in out

    def test_says_so_when_it_could_not_pin(self, tmp_path, monkeypatch):
        """Printing "pinned" over a workflow tracking a moving branch is how an
        operator stops checking it — and `main` moving is the whole failure."""
        _fake_ref(monkeypatch, "main", config_repo._UNPINNED_NOTE.format(
            reason="nerve was not installed from a git checkout", default="main",
        ), False)
        out = self._run(["config", "init-repo", "--workspace", str(tmp_path)]).output
        assert "could NOT be pinned" in out
        assert "'main', which moves" in out

    def test_claims_nothing_about_a_workflow_it_left_alone(self, tmp_path):
        wf = tmp_path / _WORKFLOW
        wf.parent.mkdir(parents=True)
        wf.write_text("name: mine\n", encoding="utf-8")

        out = self._run(["config", "init-repo", "--workspace", str(tmp_path)]).output

        assert "already existed and was left as-is" in out
        assert "pinned to" not in out

    def test_the_verify_command_matches_what_ci_runs(self, tmp_path):
        """Before this, the runbook handed the operator a laxer command than the
        gate it feeds: a typo'd key passed locally and red-X'd on the PR."""
        out = self._run(["config", "init-repo", "--workspace", str(tmp_path)]).output
        printed = next(
            ln for ln in out.splitlines() if "nerve config validate" in ln
        )
        for flag in shlex.split(_validate_step(tmp_path)["run"]):
            if flag.startswith("--") and flag != "--workspace":
                assert flag in printed, f"{flag} missing from: {printed}"

    def test_missing_workspace_errors(self, tmp_path):
        result = self._run(
            ["config", "init-repo", "--workspace", str(tmp_path / "nope")]
        )
        assert result.exit_code != 0
        assert "does not exist" in result.output

    def test_no_config_no_workspace_errors(self, monkeypatch):
        # When config can't load (self-diagnosing → config=None) and no
        # --workspace is given, the command must fail with a clear message
        # rather than crash on the missing config.
        import nerve.cli as cli

        def _raise(config_dir=None):
            raise cli.ConfigError("boom")

        monkeypatch.setattr(cli, "load_config", _raise)
        result = CliRunner().invoke(cli.main, ["config", "init-repo"])
        assert result.exit_code != 0
        assert "Config could not be loaded" in result.output

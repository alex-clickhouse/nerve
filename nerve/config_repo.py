"""Scaffold a workspace into a shareable git config repo.

``nerve config init-repo`` drops the files a config repo needs — the CI
validation workflow, a secrets-aware ``.gitignore``, a README, and a portable
settings layer — into the workspace, after which the CLI prints the remaining
manual git/``gh`` + instance steps. Scaffolding is **idempotent**: an existing
file is never overwritten (it's reported as skipped), so re-running is safe and
won't clobber local edits.

The workspace root *is* the config repo root (it holds ``config/`` and
``skills/``), so these files land at the top level of the workspace.
"""

from __future__ import annotations

import importlib.resources
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

# Destination path (relative to the workspace root) -> source path under
# nerve/templates/. Order is display order.
_SCAFFOLD: dict[str, str] = {
    ".github/workflows/validate-config.yml": "config-repo/validate-config.yml",
    ".gitignore": "config-repo/gitignore",
    "README.md": "config-repo/README.md",
    # The same commented scaffold `nerve init` writes. A repo with no config/ at
    # all fails the workflow above on its very first commit: that job validates
    # the portable layer only, and finding no file to open is an error there, not
    # a pass. An instance's workspace already has this file, and it is skipped.
    "config/settings.yaml": "config/settings.yaml",
}

# Stands in for the nerve revision CI checks out to validate with, and for the
# comment explaining where that revision came from. Both are substituted when the
# workflow is written; the placeholders are shaped so the template itself is still
# parseable YAML.
_REF_PLACEHOLDER = "__NERVE_REF__"
_NOTE_PLACEHOLDER = "# __NERVE_REF_NOTE__"

_DEFAULT_REF = "main"

# One paragraph per line; the renderer wraps them to the comment width.
_PINNED_NOTE = (
    "`ref` is pinned to the nerve revision this instance runs, which is what "
    "makes --strict-keys below safe: an older validator rejects keys the instance "
    "understands, and a newer one accepts keys it doesn't. Bump it when you "
    "upgrade the instance — `nerve config init-repo` will not, it never "
    "overwrites this file."
)

_UNPINNED_NOTE = (
    "`ref` is NOT pinned: {reason}. It tracks the default branch instead, and "
    "that branch moves — so --strict-keys below can turn a key renamed upstream "
    "into a hard error against a config repo that is perfectly valid, months "
    "after anyone here changed anything. Replace `{default}` with the tag or SHA "
    "of the nerve you deploy, and bump it when you upgrade."
)


@dataclass
class ScaffoldResult:
    """What ``scaffold_config_repo`` did (or would do, under ``dry_run``)."""

    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    is_git_repo: bool = False
    #: The revision written into the CI workflow's ``ref:``, and whether that is
    #: a real pin or the moving default branch. Empty when the workflow already
    #: existed: its ``ref:`` is then whatever wrote it, which this run has no
    #: business describing — and the caller shouldn't claim otherwise.
    validator_ref: str = ""
    validator_pinned: bool = False


def _template_dir() -> Path:
    """Resolve nerve/templates/ in both source and installed layouts."""
    try:
        ref = importlib.resources.files("nerve") / "templates"
        p = Path(str(ref))
        if p.is_dir():
            return p
    except (TypeError, FileNotFoundError):
        pass
    p = Path(__file__).parent / "templates"
    if p.is_dir():
        return p
    raise FileNotFoundError("nerve templates not found")


def _git(args: list[str], cwd: Path) -> str | None:
    """Run a read-only git command in ``cwd``; ``None`` if it could not run."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _source_root() -> Path:
    """Where nerve's own source lives — a git checkout on a normal install."""
    return Path(__file__).resolve().parent.parent


def _validator_ref() -> tuple[str, str, bool]:
    """What CI should check nerve out at: ``(ref, explanatory note, pinned)``.

    The pin is what makes ``--strict-keys`` in the generated workflow safe — an
    older validator rejects keys the instance understands. The instance's own
    source checkout answers it, but only when there *is* one, when its commit is
    on a remote CI could fetch, and when the tree isn't carrying uncommitted
    changes the commit doesn't describe. A commit no remote has is worse than no
    pin at all: ``actions/checkout`` fails hard on an unfetchable ref, so every
    PR breaks at that step, and re-running this command cannot repair it because
    it never overwrites the file.

    So the note is returned alongside the ref and goes into the file. A workflow
    that calls itself pinned when it is tracking a moving branch is the version
    skew this was supposed to prevent, arriving later and blamed on the config.
    """
    source_root = _source_root()

    def unpinned(reason: str) -> tuple[str, str, bool]:
        note = _UNPINNED_NOTE.format(reason=reason, default=_DEFAULT_REF)
        return _DEFAULT_REF, note, False

    if not (source_root / ".git").exists():
        return unpinned("nerve was not installed from a git checkout")
    sha = _git(["rev-parse", "HEAD"], source_root)
    if not sha:
        return unpinned("the nerve checkout's current revision could not be read")
    # Tracked files only: enumerating untracked ones costs seconds on a network
    # filesystem, and a build artefact nobody committed isn't what makes the
    # commit a lie about the running code — an edited source file is.
    if _git(["status", "--porcelain", "--untracked-files=no"], source_root):
        return unpinned(
            f"the nerve checkout has uncommitted changes, so {sha[:12]} does not "
            f"describe what this instance is running"
        )
    # Reachability is judged against the remote-tracking refs this checkout
    # already has — no network call, and erring towards "not pinned" is the safe
    # direction: the cost is a moving ref the note tells you to replace, where
    # the cost of guessing wrong the other way is a config repo that cannot run
    # its own CI at all.
    if not _git(["branch", "-r", "--contains", sha], source_root):
        return unpinned(
            f"{sha[:12]} is on no remote branch this checkout knows of, and CI "
            f"cannot check out a commit it cannot fetch"
        )
    return sha, _PINNED_NOTE, True


def _render_workflow(body: str, ref: str, note: str) -> str:
    """Fill the workflow template's revision and its explanatory comment.

    The note is wrapped here rather than stored pre-wrapped, because half of it
    is an interpolated reason of unpredictable length.
    """
    out = []
    for line in body.splitlines(keepends=True):
        if line.strip() == _NOTE_PLACEHOLDER:
            indent = line[: len(line) - len(line.lstrip())] + "# "
            out.append(textwrap.fill(
                note, width=79, initial_indent=indent, subsequent_indent=indent,
            ) + "\n")
        else:
            out.append(line.replace(_REF_PLACEHOLDER, ref))
    return "".join(out)


def scaffold_config_repo(workspace: Path, dry_run: bool = False) -> ScaffoldResult:
    """Write the config-repo scaffold files into ``workspace``.

    Never overwrites an existing file (reported in ``skipped``). With
    ``dry_run=True`` nothing is written but the created/skipped split is still
    computed. Returns a :class:`ScaffoldResult`.
    """
    workspace = Path(workspace)
    tmpl = _template_dir()
    result = ScaffoldResult(is_git_repo=(workspace / ".git").exists())

    for rel, src_name in _SCAFFOLD.items():
        dst = workspace / rel
        if dst.exists():
            result.skipped.append(rel)
            continue
        result.created.append(rel)
        body = (tmpl / src_name).read_text(encoding="utf-8")
        if _REF_PLACEHOLDER in body:
            # Resolved under dry_run too — "which revision would this pin to, and
            # would it be a pin at all" is exactly what a preview is for.
            ref, note, pinned = _validator_ref()
            result.validator_ref, result.validator_pinned = ref, pinned
            body = _render_workflow(body, ref, note)
        if dry_run:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(body, encoding="utf-8")

    return result

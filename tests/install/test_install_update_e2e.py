"""Real install/update E2E: can a user on main reach this commit?

This drives ``scripts/dev-sandbox.sh``, the project's existing fake Internet --
a bubblewrap sandbox with no writable host mounts, a static MITM proxy serving
the canonical install.sh URL, and a git-upload-pack shim standing in for
github.com. We deliberately do NOT hand-roll a second harness: the sandbox
already exercises the real entry points, so this is a thin driver over it.

What actually runs:

1. ``dev-sandbox.sh install --from-main`` fetches genuine upstream main, serves
   its ``install.sh`` at the real URL, and the sandbox runs the true one-liner
   (``curl -fsSL https://…/install.sh | bash``). That performs a real install --
   uv, a managed Python, Node, the venv -- cloning "github.com" through the ssh
   shim. On success the sandbox promotes *this* checkout to fake main, leaving
   exactly the state a user is in when an update is waiting.
2. ``hermes --version`` must work (proves the venv + entry point are live).
3. The update route must land the checkout on fake main's commit.
4. ``hermes --version`` must still work afterwards.

Routes covered, per docs/plans/2026-08-03-update-path-e2e-testing-plan.md:
route 2 (``hermes update``) and route 1 (installer re-run).

Excluded from default discovery via ``_SKIP_PARTS`` in
``scripts/run_tests_parallel.py``: a run installs real toolchains and takes
minutes. Run it deliberately:

    HERMES_RUN_INSTALL_E2E=1 scripts/run_tests.sh tests/install/

Requires bubblewrap, slirp4netns and unprivileged user namespaces, so it is
gated on those rather than failing obscurely without them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX_SCRIPT = REPO_ROOT / "scripts" / "dev-sandbox.sh"


def _sandbox_command() -> list[str] | None:
    """How to invoke the sandbox here, or None when it isn't available.

    Prefer the ``sandbox`` wrapper from the Nix devShell: it is the supported
    entry point and supplies both the PATH (bwrap, slirp4netns, openssl, ...)
    and the DEV_SANDBOX_* variables the script needs -- notably
    DEV_SANDBOX_DYNAMIC_LINKER, without which the script cannot find a glibc
    loader on NixOS. Calling the script directly under Nix fails with a bare
    exit 127.

    Off Nix (a CI runner with apt-installed bubblewrap), the script itself is
    the entry point and finds everything on the system PATH.
    """
    wrapper = shutil.which("sandbox")
    if wrapper:
        return [wrapper]
    if SANDBOX_SCRIPT.is_file() and shutil.which("bwrap"):
        return [str(SANDBOX_SCRIPT)]
    return None

# A cold run installs uv, a managed CPython, and Node from the network, then
# does it again for the update. Generous, but bounded so a wedged job dies.
INSTALL_TIMEOUT_SECONDS = 25 * 60
SHELL_TIMEOUT_SECONDS = 20 * 60

# dev-sandbox.sh joins HERMES_DEV_SANDBOX_DIR onto the worktree root and feeds
# it to `tar --exclude`, so it must be a RELATIVE directory name. An absolute
# path would create dirs inside the worktree and defeat the self-recursion
# guard when the sandbox copies the repo. Distinct from the default
# .hermes-sandbox so a run never clobbers a developer's own sandbox.
SANDBOX_DIR_NAME = ".hermes-sandbox-e2e"

# The sandbox installs as an unprivileged `hermes` user by default -- the layout
# most Linux users get (code under $HERMES_HOME, launcher in ~/.local/bin).
SANDBOX_INSTALL_DIR = "/home/hermes/.hermes/hermes-agent"
FAKE_REMOTE_GIT_DIR = "/work/repos/hermes-agent.git"

# Printed by the in-sandbox shell so a failure says which step broke.
MARKER_OK = "E2E-MARKER-OK"


def _worktree_is_dirty() -> bool:
    """True when the checkout has staged, unstaged, or untracked changes."""
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Can't tell; let the test proceed rather than skip on a probe failure.
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _missing_requirement() -> str | None:
    """Why this environment cannot host the E2E, or None when it can."""
    if not os.environ.get("HERMES_RUN_INSTALL_E2E"):
        return (
            "set HERMES_RUN_INSTALL_E2E=1 to run the real install/update E2E "
            "(installs toolchains over the network; takes minutes)"
        )
    if os.name != "posix":
        return "install.sh and the sandbox are POSIX-only"
    if _sandbox_command() is None:
        return (
            "no usable sandbox: need the `sandbox` wrapper from the Nix "
            "devShell, or bubblewrap on PATH alongside scripts/dev-sandbox.sh"
        )
    if _worktree_is_dirty():
        # Not fussiness: with a dirty tree, EVERY dev-sandbox invocation builds
        # a fresh snapshot commit of the working copy and force-moves fake main
        # onto it. The update target therefore changes between the call that
        # installs and the call that verifies, so "did we land on fake main?"
        # compares against a moving reference and fails for the wrong reason.
        # CI always runs on a clean checkout; locally, commit or stash first.
        return (
            "working tree is dirty; the sandbox would re-snapshot it into a new "
            "fake-main commit on every invocation, moving the update target "
            "mid-test. Commit or stash first."
        )
    return None


pytestmark = [
    pytest.mark.skipif(
        _missing_requirement() is not None,
        reason=_missing_requirement() or "",
    ),
    # tests/conftest.py's live-system guard blocks subprocesses that look like
    # they would mutate the developer's real install -- and `hermes update` is
    # exactly that shape. Here the command only ever reaches a throwaway
    # checkout inside the bubblewrap sandbox, which has no writable host
    # mounts, so the guard is a false positive. This is the case its own error
    # message sanctions: "an integration test testing the update flow against a
    # dedicated throwaway repo".
    pytest.mark.live_system_guard_bypass,
]


def _run_sandbox(args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    command = _sandbox_command()
    assert command is not None, "sandbox unavailable; the skip guard should have caught this"

    env = dict(os.environ)
    env["HERMES_DEV_SANDBOX_DIR"] = SANDBOX_DIR_NAME

    return subprocess.run(
        [*command, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _fail(completed: subprocess.CompletedProcess, what: str) -> None:
    pytest.fail(
        f"{what} failed (exit {completed.returncode})\n"
        f"--- stdout ---\n{completed.stdout}\n"
        f"--- stderr ---\n{completed.stderr}",
        pytrace=False,
    )


def _sandbox_shell(script: str, what: str) -> str:
    """Run a script inside the persistent sandbox; require the OK marker."""
    completed = _run_sandbox(
        ["--persistent", "bash", "-lc", script],
        timeout=SHELL_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0 or MARKER_OK not in completed.stdout:
        _fail(completed, what)
    return completed.stdout


def _install(from_main: bool, what: str) -> subprocess.CompletedProcess:
    args = ["install", "--persistent"]
    if from_main:
        args.append("--from-main")
    # Sandbox flags must precede `--`; everything after it goes to install.sh.
    args += ["--", "--skip-setup", "--skip-browser"]

    completed = _run_sandbox(args, timeout=INSTALL_TIMEOUT_SECONDS)
    if completed.returncode != 0 or "Installation Complete" not in completed.stdout:
        _fail(completed, what)
    return completed


@pytest.fixture(scope="module")
def installed_base() -> Iterator[str]:
    """Install genuine upstream main; return the commit it landed on.

    Module-scoped because a real install costs minutes and both routes start
    from the same state.
    """
    sandbox_root = REPO_ROOT / SANDBOX_DIR_NAME
    if sandbox_root.exists():
        shutil.rmtree(sandbox_root, ignore_errors=True)

    completed = _install(from_main=True, what="install of upstream main")
    assert "fake main advanced to this folder" in completed.stderr, (
        "sandbox did not promote this checkout to fake main, so there is "
        "nothing for the update routes to reach:\n" + completed.stderr
    )

    out = _sandbox_shell(
        f"""
set -euo pipefail
cd {SANDBOX_INSTALL_DIR}
echo "base=$(git rev-parse HEAD)"
echo "target=$(git --git-dir={FAKE_REMOTE_GIT_DIR} rev-parse main)"
echo '--- hermes --version (after install)'
hermes --version
echo {MARKER_OK}
""",
        "post-install verification",
    )

    base = _field(out, "base")
    target = _field(out, "target")
    assert base != target, (
        f"install landed on the update target ({base}); the base and the "
        "commit under test must differ for these routes to prove anything"
    )

    yield base

    shutil.rmtree(sandbox_root, ignore_errors=True)


def _field(output: str, key: str) -> str:
    """Pull a ``key=value`` line out of sandbox output."""
    for line in output.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"no {key}= line in sandbox output:\n{output}")


def _reset_to(commit: str) -> None:
    _sandbox_shell(
        f"""
set -euo pipefail
cd {SANDBOX_INSTALL_DIR}
git reset -q --hard {commit}
echo "at=$(git rev-parse HEAD)"
echo {MARKER_OK}
""",
        f"reset checkout to {commit[:12]}",
    )


def test_hermes_update_reaches_this_commit(installed_base: str) -> None:
    """``hermes update`` moves an upstream-main install onto this commit.

    Asserts ``hermes --version`` works after the update too, so a green result
    means the venv and entry point survived it -- not merely that git moved.
    """
    _reset_to(installed_base)

    out = _sandbox_shell(
        f"""
set -euo pipefail
cd {SANDBOX_INSTALL_DIR}
target=$(git --git-dir={FAKE_REMOTE_GIT_DIR} rev-parse main)
echo "before=$(git rev-parse HEAD)"

echo '--- hermes update --yes'
hermes update --yes

after=$(git rev-parse HEAD)
echo "after=$after"
[ "$after" = "$target" ] || {{ echo "update left HEAD at $after, wanted $target"; exit 1; }}

echo '--- hermes --version (after update)'
hermes --version
echo {MARKER_OK}
""",
        "hermes update route",
    )
    assert _field(out, "before") == installed_base
    assert _field(out, "after") != installed_base


def test_installer_rerun_reaches_this_commit(installed_base: str) -> None:
    """Re-running the real install.sh one-liner also reaches this commit.

    The path a user takes when they re-run the curl one-liner instead of
    ``hermes update``: install.sh finds an existing checkout, autostashes, and
    pulls.
    """
    _reset_to(installed_base)

    # No --from-main: this serves the worktree's own installer and points fake
    # main at this checkout, which is what the re-run must land on.
    _install(from_main=False, what="installer re-run")

    # Read the target AFTER the install, never before: each dev-sandbox
    # invocation re-derives fake main from the worktree, so a target captured
    # earlier can be stale by the time the re-run finishes (it is a fresh
    # snapshot commit whenever the tree is dirty). Comparing HEAD against the
    # remote's CURRENT main is the invariant that actually holds.
    out = _sandbox_shell(
        f"""
set -euo pipefail
cd {SANDBOX_INSTALL_DIR}
target=$(git --git-dir={FAKE_REMOTE_GIT_DIR} rev-parse main)
after=$(git rev-parse HEAD)
echo "after=$after"
echo "target=$target"
[ "$after" = "$target" ] || {{ echo "re-run left HEAD at $after, wanted $target"; exit 1; }}
echo '--- hermes --version (after installer re-run)'
hermes --version
echo {MARKER_OK}
""",
        "installer re-run verification",
    )
    assert _field(out, "after") != installed_base

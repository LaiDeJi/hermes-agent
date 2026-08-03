# Update-path E2E testing — plan

**Status:** v1 in progress (routes 1 & 2). Linux only.
**Goal:** prove in CI that a user on `main` can get to this PR's commit, by
every update route we support.

## Why

We have ~17k unit tests and zero coverage of the thing that breaks worst: the
update. A broken updater is uniquely bad — it strands users on the version that
cannot fix itself. `hermes update` alone is ~2000 lines
(`hermes_cli/update_cmd.py`), and nothing exercises it end to end.

The shape of the test is always the same:

1. Build a fake `main` = real upstream `main` + this PR's commit merged in.
2. Install *real upstream `main`* the way a user would.
3. Update via the route under test.
4. Assert HEAD landed on the merge commit and the install still works
   (`hermes --version`, `hermes doctor`).

`scripts/dev-sandbox.sh` already does 1 and 2 (`install --from-main` fetches
genuine upstream main, parks this folder at `refs/hermes-sandbox/next`, and
promotes it to fake `main` after install — exactly the "newer main is waiting"
state an update needs).

## The routes

Grounded in code, not guessed. Linux only; Windows/macOS deferred.

| # | Route | Entry point | v1? |
|---|-------|-------------|-----|
| 1 | Re-run `install.sh` over an existing checkout | `scripts/install.sh` (autostash → pull → deps) | **yes** |
| 2 | `hermes update` | `update_cmd.py::_cmd_update_impl` | **yes** |
| 3 | Desktop app "Update" button | `apps/desktop/electron/main.ts::applyUpdatesPosixInApp` → `hermes update --yes --branch <healed>` then `hermes desktop --build-only` | no |
| 4 | `/update` from a messaging platform | `gateway/run.py` → `_handle_update_command` (gateway-mode file-IPC prompts) | no |
| 5 | Manual `git pull` + `uv pip install -e ".[all]"` on a self-managed checkout | docs `updating.md` "Manual Update" | no |
| 6 | Docker — `docker pull` | `config.py::_DOCKER_UPDATE_MESSAGE`; `hermes update` **refuses** | no |
| 7 | Nix — `nix profile upgrade` / flake rebuild | `config.py::_NIX_UPDATE_MSG`; `hermes update` **refuses** | no |

Routes 6 and 7 are refusal paths: the useful assertion is that `hermes update`
*declines with the right guidance*, which is cheap and unit-testable — it hangs
off `detect_install_method()` (`hermes_cli/config.py:410`).

### Install-layout axis

Orthogonal to the route, and easy to miss: `install.sh` picks its layout from
`id -u` alone (`resolve_install_layout`, `install.sh:422`).

| uid | Code | Command | Data |
|-----|------|---------|------|
| non-root | `$HERMES_HOME/hermes-agent` | `~/.local/bin/hermes` | `~/.hermes` |
| root (Linux) | `/usr/local/lib/hermes-agent` | `/usr/local/bin/hermes` | `/root/.hermes` |

Both need coverage. `dev-sandbox.sh` now defaults to the user-level layout (what
most people run) with `--root` for FHS. Note the root path also redirects
`UV_PYTHON_INSTALL_DIR` to `/usr/local/share/uv` for world-readability
(#21457), so the two layouts differ in more than paths.

### How the user-level sandbox gets a network

Worth recording, because the failure is non-obvious. slirp4netns joins the
target's userns and setuids to root before configuring the netns, so the userns
**must map a uid 0**. bwrap's `--unshare-user` maps exactly one uid, so
`--uid 1000` left no root for slirp to become and it died with
`setns(CLONE_NEWNET): Operation not permitted`.

The script now creates the namespaces itself in a stage-1 launcher, with two
one-id ranges:

```
inner 0     <- a subuid   (never used by the payload; exists so slirp can be root)
inner 1000  <- our real host uid
```

Mapping inner 1000 to the *host* uid (rather than another subuid) is what keeps
everything the sandbox writes owned by us, so `rm -rf .hermes-sandbox` still
works with no chown dance. Stage 2 then runs bwrap **without** `--unshare-user`
— it only adds the mount/pid namespaces — which sidesteps bwrap's refusal to
accept `--uid` outside a userns it created. `unshare --user` grants its creator
full capabilities in the new userns regardless of mapped uid, so bwrap can still
mount as uid 1000.

Cost: a `/etc/subuid` + `/etc/subgid` range for the invoking user (the script
errors with the exact line to add if missing) and util-linux `unshare`. `--root`
needs neither. **CI implication:** GitHub runners need to be checked for subuid
allocations before Tier B can use the default mode; if they lack them, Tier B
runs `--root` and Tier A covers the user-level layout.

## Two tiers of test

**One harness, not two.** `scripts/dev-sandbox.sh` is the harness. It already
runs the true `curl … | install.sh` one-liner over a MITM proxy at the canonical
URL, clones "github.com" through a `git-upload-pack` shim (exercising the
ssh-first-then-https fallback), writes nothing outside `SANDBOX_ROOT`, and
implements the install-main-then-update state machine via `--from-main`.

An earlier draft of this work built a second, sandbox-free harness that rewrote
the installer's hardcoded URLs with `url.<file://…>.insteadOf` and ran
`install.sh` directly against the host. It was deleted. It was strictly worse on
the axis that matters — it invoked `bash install.sh` instead of the real
one-liner, needed `GIT_SSH_COMMAND=false` to stop a failed rewrite reaching real
GitHub, and installed toolchains against the *host's* libraries, so it validated
NixOS glibc locally and Ubuntu's on CI rather than a clean machine. Maintaining
a second fake Internet to test the installer less faithfully is a bad trade; the
recurring "add another binary to the allowlist PATH" churn was the symptom.

The remaining tiering is about *where it runs*, not *how*:

- **Local / Nix** — `HERMES_RUN_INSTALL_E2E=1 scripts/run_tests.sh tests/install/`.
  Uses the `sandbox` wrapper from the devShell.
- **CI** — needs `bubblewrap` + `slirp4netns` + `util-linux` installed and
  unprivileged userns permitted. Unverified; see Open items.

## Test layout

`tests/install/` — a sibling of the existing opt-out suites, registered in
`_SKIP_PARTS` in `scripts/run_tests_parallel.py` alongside `integration`, `e2e`,
and `docker`. That machinery already does exactly what this suite needs: absent
from default discovery (a run installs real toolchains over the network and
takes minutes) while still runnable by naming the path explicitly.

Two further gates, because being merely excluded is not enough:

- `HERMES_RUN_INSTALL_E2E=1` is required, so an explicit
  `scripts/run_tests.sh tests/install/` on a developer machine skips rather
  than silently burning ten minutes. It is threaded through `run_tests.sh`'s
  `env -i` allowlist (that script scrubs the environment for CI parity, so an
  unlisted var never reaches pytest — the reason the first attempt skipped).
- The sandbox must actually be usable. The probe prefers the `sandbox` wrapper
  and only falls back to the raw script when `bwrap` is on PATH; under Nix the
  script alone exits 127 because the wrapper is what supplies the PATH and the
  `DEV_SANDBOX_*` variables.

## Known traps

- **Pipe-hides-failure.** `sandbox install … | tail` reports exit 0 even when
  the installer dies; the symptom is a confusing `curl: (23) Failure writing
  output to destination`. Any CI script needs `set -o pipefail`.
- **Sandbox flag ordering.** `dev-sandbox.sh` stops parsing at the first
  argument it doesn't recognize and passes the rest through, so sandbox flags
  must precede `--`. `install --skip-setup --from-main` sends `--from-main` to
  the installer, which rejects it.
- **`insteadOf` is multi-valued.** A second plain `git config` on the same key
  *replaces* rather than appends, silently letting one URL escape to real
  GitHub. Use `--add` and assert both rewrites with `--get-all`.
- **Shallow clones.** The installer clones `--depth 1`; the updater fetches a
  single scoped ref. Any fixture must reproduce both or it tests nothing real.
- **A dirty worktree changes fake main.** dev-sandbox snapshots uncommitted
  changes into a temp commit, so CI must either commit first or expect the
  snapshot SHA, not `HEAD`.
- **`--commit` never rolls backwards** without `--force-commit`
  (`install.sh:1382`) — an installer-driven test that pins an older commit
  will silently no-op.

## Open items

- **Does bwrap work on a GitHub runner?** The gating unknown. Needs
  `bubblewrap` + `slirp4netns` + `util-linux` and unprivileged user namespaces;
  ubuntu-24.04 restricts those via AppArmor by default, so the job may need
  `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`. The
  user-level default additionally wants an `/etc/subuid` range for the runner
  user — if runners lack one, the CI job can pass `--root` (the script errors
  with the exact line to add either way). Settle this with a throwaway
  workflow before building the real job around it.
- **Routes 3–7.** Deferred (see table). Route 3 is the highest-value next one,
  since the desktop button is how most non-terminal users update; it shells the
  same `hermes update`, so route 2's coverage carries most of the risk.
- **macOS / Windows.** Out of scope for now. The Windows installer path had
  prior AutoHotkey automation on a branch (`ef7749d4c`, never merged to main);
  worth revisiting once Linux is green.

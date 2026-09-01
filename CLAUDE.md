# CLAUDE.md

Notes for anyone — human or otherwise — working in this repository.

User-facing documentation lives in [README.md](README.md): every environment
variable, the DKIM/SRS/SASL/rsyslog setup, the security guidance, the health
check and the deployment examples are documented there, and its
[Testing](README.md#testing) section is the source for the commands below.
This file does not repeat any of it — it covers what the README does not: the
real layout, what gates a merge, and the decisions in the code that look like
bugs but are not.

## What this is

One Docker image: a Postfix SMTP relay for other containers to send mail
through, plus optional OpenDKIM signing, optional SRS rewriting, SASL
authentication on both sides and rsyslog forwarding. Everything is configured
through environment variables, which the `run` entrypoint translates into
config files at container start. A second script, `healthcheck`, is what
docker's `HEALTHCHECK` invokes.

Published as `mwader/postfix-relay`. Issue and pull request numbers cited below
are this repository's own tracker; contributor branches live in forks, which is
why merge commits name someone else's namespace.

## Layout

| Path | Role |
| --- | --- |
| `Dockerfile` | Debian base pin (`FROM debian:trixie-<date>-slim`), the apt packages, the conditional `postsrsd` install, the build-time deletion of `/etc/rsyslog.conf` and `/etc/postsrsd.secret`, the default `ENV` block, `COPY run healthcheck /root/`, `VOLUME`, `EXPOSE 25`, `HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["/root/healthcheck"]` and `CMD ["/root/run"]`. No `ENTRYPOINT`, no `ARG`. |
| `run` | The entrypoint. Resolves `<NAME>_FILE` secrets, turns `POSTFIX_*`, `POSTFIXMASTER_*`, `POSTMAP_*`, `OPENDKIM_*`, `POSTSRSD_*`, `RSYSLOG_*`, `SASL_Passwds` and `POSTMASTER_ADDRESS` into config, starts the daemons, asks postfix for an SMTP greeting, then runs a `pgrep`-polling supervision loop. Nearly all behaviour lives here. |
| `healthcheck` | `pgrep`s `master`, checks a listening socket for every `inet` service in `postconf -M`, then `rsyslogd` always, `opendkim`/`postsrsd` whenever the environment *or the artefacts start-up left behind* say so, and `saslauthd` when `SASL_Passwds` is set. |
| `pytest.ini` | `addopts = -n auto --dist loadfile --maxprocesses 4` and one registered marker, `smoke`. No `testpaths`, no `filterwarnings`, no `xfail_strict`. |
| `.dockerignore` | Keeps `.git`, `README.md`, `LICENSE`, `tests` and `pytest.ini` out of the build context. |
| `tests/__init__.py` | Empty; makes `tests` a package, which is what lets `conftest.py` name plugins as `tests.fixtures.*` and lets modules do `from tests.helpers import …`. pytest therefore has to be run from the repo root. There is no `tests/fixtures/__init__.py`. |
| `tests/conftest.py` | Registers the four fixture modules as pytest plugins, and defines the failure plumbing: `print_log_on_failure`, an autouse `shared_container_logs` fixture, and a `pytest_runtest_makereport` wrapper hook that stashes the report on the item. |
| `tests/helpers.py` | The shared vocabulary — `poll_until`, `once_across_workers`, `wait_for_smtp`, `send`, `container_exec`, `postconf`, `listening_ports`, `exit_code_within` and the rest. Imported by every test module but `test_sendmail.py`, and by three of the four fixture modules. `file_missing` is defined and never called. |
| `tests/requirements.txt` | Six pinned packages: `dkimpy`, `docker`, `pytest`, `pytest-xdist`, `requests`, `testcontainers[mailpit]`. `dkimpy` is what satisfies `import dkim`, so grepping module names against this file looks like a miss when it is not. |
| `tests/fixtures/shared_network.py` | Session-scoped `shared_network`: a testcontainers `Network()` with a generated, labelled name — not a fixed one, so an interrupted run leaves nothing for the next one to collide with. |
| `tests/fixtures/postfix.py` | Six fixtures: `postfix_image`, `postfix` and `_relay_pool` (session, the last being the per-configuration relay pool); `postfix_factory`, `postfix_shared` and `docker_volume` (function). Every relay gets `POSTFIX_relayhost=mailpit:1025`, set before the caller's env so a test can override it; readiness is an SMTP connection, not a log line. Reads `POSTFIX_RELAY_IMAGE` / `POSTFIX_RELAY_ARCH`. |
| `tests/fixtures/mailpit.py` | The remote-SMTP stand-in, pinned at `axllent/mailpit:<tag>`: a `Mailpit` REST client class plus `mailpit_image` and `mailpit_container` (session), `mailpit` and `mailpit_factory` (function). Also rebinds the library's `wait_for_logs` to a `functools.partial` with a shorter poll interval. |
| `tests/fixtures/smtp.py` | `smtplib` client against the shared `postfix` relay's mapped port 25. Function-scoped on purpose: the tests about rejected mail leave the connection broken. |
| `tests/test_*.py` | `capabilities`, `client_tls`, `config`, `defaults`, `dkim`, `healthcheck`, `image`, `lifecycle`, `logging`, `postmaster`, `qshape`, `sasl`, `secrets`, `sendmail`, `smtp`, `srs`. The README's per-file table covers all but `test_qshape.py` and `test_secrets.py`. |
| `tests/img/postfix-logo.png` | The inline image `tests/test_sendmail.py` attaches, and compares byte for byte on the way out. |
| `.github/workflows/ci.yml` | `name: ci`. One job, `docker`, displayed as **Build Image**: buildx over `linux/amd64,linux/arm/v7,linux/arm64/v8`, GHA build cache, nothing pushed on a pull request. |
| `.github/workflows/test.yml` | `name: test`. Four jobs: **Event File**, **Pytest**, **Pytest (arm64)** and **Pytest (arm/v7, emulated)**. Spelled out rather than written as a matrix; the file says why. |
| `.github/workflows/test-results.yml` | On `workflow_run` of `test`, downloads the junit artifacts and publishes them as the **Test Results** check. |
| `.github/workflows/dependabot-auto-merge.yml` | On `pull_request`, for `dependabot[bot]` only: enables auto-merge for semver-minor and semver-patch updates. Its header comment is also where the check names are recorded. |
| `.github/dependabot.yml` | `github-actions` weekly (grouped minor/patch and major), `docker` daily for the base image. No pip ecosystem. |

There is no `CONTRIBUTING.md`, no linter config of any kind and no per-file
license header — see [Conventions](#conventions).

## Dependency graph

```
Dockerfile ──FROM──> debian:trixie-<date>-slim
    │                  (pinned; bumped daily by .github/dependabot.yml,
    │                   docker ecosystem — no Bumpfile, no "# bump:" line)
    ├──COPY──> run          -> /root/run          <- CMD
    └──COPY──> healthcheck  -> /root/healthcheck  <- HEALTHCHECK

.dockerignore excludes .git, README.md, LICENSE, tests, pytest.ini
    => nothing under tests/, and not pytest.ini, can invalidate the build cache

pytest.ini ──addopts──> pytest-xdist   (-n auto --dist loadfile --maxprocesses 4)
    => a pytest without the plugin does not start at all

tests/test_*.py
    │
    ├──> tests/helpers.py         (every module but test_sendmail.py)
    │        └── once_across_workers  -> builds/pulls the image once per RUN,
    │                                     not once per xdist worker
    └──> fixtures, registered in tests/conftest.py as pytest_plugins:

         smtp (function) ──> postfix (session) ──┐
         postfix_factory (function) ─────────────┤
         postfix_shared (function) ──> _relay_pool (session) ──┤
                                                               ▼
                                        postfix_image (session)
                                          ├── docker build .  -> postfix-relay:test
                                          │      -> Dockerfile -> run, healthcheck
                                          └── or POSTFIX_RELAY_IMAGE, but only for
                                              an architecture this host cannot build

         mailpit (function) ──> mailpit_container (session) ──> mailpit_image (session)
         mailpit_factory (function) ──────────────────────────────┘

         docker_volume (function)   -- takes no fixtures; creates and removes
                                       a docker volume and nothing else

         every container ──> shared_network (session, testcontainers Network())
```

The test suite is the only consumer of the image inside this repo. `run` has no
caller other than the `CMD` — and one test, which re-execs it after deleting
the `postsrsd` binary. `healthcheck` is invoked by docker and directly by
`test_healthcheck.py`, `test_capabilities.py`, `test_secrets.py` and
`tests/helpers.py`.

## Commands

The image build and the tests both need a working Docker daemon. The tests
build the image themselves — you never need to build it first.

### Tests

Per [README.md](README.md#testing), in a virtualenv:

```bash
python -m venv venv && source venv/bin/activate
pip install -r tests/requirements.txt
pytest
```

Useful invocations:

```bash
pytest -v                                    # per-test names
pytest tests/test_dkim.py                    # a single file
pytest tests/test_dkim.py::test_the_signature_verifies_against_the_published_key
pytest -n0                                   # one process, easier to follow
pytest --junitxml=junit/test-results.xml     # exactly what CI runs
pytest -m smoke                              # the four smoke tests
pytest --collect-only -q tests               # no docker daemon needed
```

**What `pytest.ini` does to every one of those.** `addopts = -n auto --dist
loadfile --maxprocesses 4` means a bare `pytest` always runs under
pytest-xdist. That makes the plugin a hard requirement rather than an optional
extra: a pytest without it fails at start-up with `unrecognized arguments: -n`,
before importing anything. `--dist loadfile` keeps every test of a file in one
worker, which is what the per-configuration relay pooling needs to pay off, and
`auto` is the processor count capped at 4 — a measured cap, with the numbers in
`pytest.ini` itself. `-n0` is several times slower than the default, not
equivalent to it.

`pytest.ini` sets nothing else. There is no `testpaths` (collection starts from
the rootdir), and no `filterwarnings` or `-W error` anywhere in the tree, so
warnings are reported and cannot fail a run. Some come from testcontainers
itself and cannot be fixed here.

To exercise `arm/v7` locally — the only architecture the suite cannot build on
an ordinary machine — follow the cross-build recipe in
[README.md](README.md#testing). It pins the same binfmt image CI does, so a
failure there means the same thing.

There is no unit test layer: every test starts real containers. When a test
fails, `conftest.py` prints the logs of the containers it used, so
`-o log_cli=true` is rarely what you want.

### Build

```bash
docker build -t postfix-relay .

# what CI builds — needs QEMU binfmt registered for the non-native arches
docker buildx build --platform linux/amd64,linux/arm/v7,linux/arm64/v8 .
```

The suite builds the same context itself and tags it `postfix-relay:test`, so
building by hand is only for looking at the image. Reproducing the `arm/v7` and
`arm64/v8` legs locally requires binfmt handlers; without them a break specific
to those arches — the `postsrsd` case below is exactly that — will not show up
until CI runs.

### Lint

**There is none.** No linter config exists anywhere in the repo (no
`.shellcheckrc`, `.hadolint.yaml`, `pyproject.toml`, `setup.cfg`, `tox.ini`,
`.flake8`, `.ruff.toml` or `.pre-commit-config.yaml`), and no workflow runs
one.

For reference, with shellcheck 0.9.0 and stock defaults — there is no
`.shellcheckrc` and no in-file `# shellcheck disable=` anywhere, so nothing is
suppressed:

```bash
shellcheck run          # 15 findings: 1 warning, 9 info, 5 style; exits 1
shellcheck healthcheck  # 1 finding: SC2086 (info); exits 1
shellcheck -S error run healthcheck   # exits 0 — the only clean threshold
```

Two of those must not be "fixed": the `SC2034` in `awaitGreeting` is a retry
counter the loop body has no reason to read (the identical construct in
`awaitProcess` is not flagged, which is a quirk of the tool), and the `SC2086`
in `healthcheck` is deliberate word-splitting — `listening()` builds
`proc="/proc/net/tcp /proc/net/tcp6"` and awk must receive both paths. Quoting
it, as shellcheck suggests, breaks IPv6 listener detection.

Counts drift with every shellcheck release and nothing here pins one, so treat
them as a snapshot. Nothing enforces any of this: do not add a lint gate as a
side effect of an unrelated change — `-S warning` would fail today.

## What blocks a pull request

Three workflow files carry a `pull_request` trigger, but `dependabot-auto-merge.yml`
is a no-op for anything not opened by `dependabot[bot]` — its single job has no
display `name:`, so on an ordinary pull request it appears as a skipped
`auto-merge` check. The five that can actually fail are:

| Check | From | What it does |
| --- | --- | --- |
| **Build Image** | `ci.yml` | buildx over `linux/amd64,linux/arm/v7,linux/arm64/v8`. Nothing is pushed on a PR — the DockerHub login is skipped, and the build step sets `push: ${{ github.event_name != 'pull_request' && github.actor != 'dependabot[bot]' }}` — but the build has to succeed on **all three** architectures. The `dependabot[bot]` half of that condition covers the branch push Dependabot makes before opening its pull request: such a run reads Dependabot secrets only, so the Actions secrets the login needs arrive empty. This is the gate that catches architecture-specific packaging problems. |
| **Pytest** | `test.yml` | `ubuntu-latest`, Python 3.13, `pip install -r tests/requirements.txt`, `pytest --junitxml=junit/test-results.xml`. |
| **Pytest (arm64)** | `test.yml` | The same, natively, on `ubuntu-24.04-arm`. |
| **Event File** | `test.yml` | Uploads the triggering event payload for the reporter. |
| **Test Results** | `test-results.yml` | Runs on `workflow_run` of `test`, downloads the junit artifacts and publishes them onto the PR. |

A sixth, **Pytest (arm/v7, emulated)**, runs only on `master`, on a manual
dispatch, or on a pull request labelled `test-emulated` — and the label is read
from the event that started the run, so it takes effect on the *next* push to
the branch. It pins the QEMU binfmt image, builds `linux/arm/v7`, and runs
`pytest -m smoke -n0`.

Notes a contributor will hit:

- **Required checks match the job's `name:`**, never the workflow name and
  never the job key. Both `ci.yml` and `test.yml` still key their job `docker`;
  commit `f148d33` added the display names precisely because both reported a
  check called "docker". Which checks are actually *required* lives in a
  repository ruleset, not in this tree — the header comment in
  `dependabot-auto-merge.yml` records the candidate names and explicitly rules
  the emulated job out of the list.
- **The pytest step has `timeout-minutes: 10`** inside a 20-minute job (25/45
  for the emulated one). The job timeouts are backstops: a cancelled job skips
  the upload step, so the bound expected to fire is the step's. Every wait in
  the suite is bounded, so overrunning it means something is stuck, not slow.
- **One action is pinned to a commit**, `EnricoMi/publish-unit-test-result-action`
  in `test-results.yml`; everything else is on a major tag, and Dependabot's
  weekly `github-actions` ecosystem bumps them. Do not "normalise" that
  exception away.
- `ci.yml` runs on pushes to every branch and tag; `test.yml` runs on pushes to
  `master` only, plus `pull_request` and `workflow_dispatch`. Both cancel
  in-flight runs on any ref but `master`.
- Version skew: CI pins Python 3.13, and nothing pins a Python version locally.
  The Python dependencies are pinned exactly (`tests/requirements.txt`); their
  transitive dependencies are not, and there is no lock file, so a run can
  still break without a change in this repo.
- No workflow runs a linter, so nothing lint-shaped blocks a merge.

## Conventions

- **Commits.** Imperative, sentence-case subject describing the change
  ("Stop taking ownership of /var/mail on start-up", "Ask postfix for a
  greeting before handing over"), a blank line, then a body that explains
  *why*, what the alternative would have cost, and how the change was verified
  against a built image ("Verified on the built image: …" and "Tested on the
  built image: …" are equally common). Reference issues with `Closes #NNN` /
  `Fixes #NNN`. The commit bodies here are unusually detailed and are the
  primary record of the decisions listed below — keep that up. Pull requests
  are merged with merge commits.
- **Sign-off.** Not used. `git log --all --grep="Signed-off-by"` matches
  nothing and there is no DCO check. Do not add one.
- **License headers.** Not used. The project is MIT ([LICENSE](LICENSE)); no
  source file carries a header and `SPDX` appears nowhere in the tree. Do not
  add one to new files.
- **Shell style** (`run`, `healthcheck` — the only two shell scripts, both
  `#!/bin/bash`). No `set -e` (see below), function brace on its own line,
  `[ ... ] ; then` with spaces around the `;`, four-space bodies inside
  functions and two-space bodies in top-level blocks, and a comment above each
  block explaining the intent rather than the syntax.
- **Tests.** Behaviour the README promises is pinned by a test. Three rules the
  plumbing does not enforce for you:
  - a fixture that stops a container calls `print_log_on_failure` *first*,
    otherwise testcontainers throws away the only record of why the relay
    misbehaved;
  - every wait goes through `poll_until` (or a helper built on it) with a
    `description`, never a bare `sleep` — that is what makes the CI step
    timeout mean "stuck" rather than "slow";
  - a new test module gets a row in the README's per-file table.
  A defect that is understood but not fixed is covered by a test asserting the
  correct behaviour, marked `xfail(strict=True)` with its issue. There are no
  current instances, and `xfail_strict` is not set in `pytest.ini`, so
  `strict=True` has to be written on each marker.
- **Docs.** User-visible behaviour goes in `README.md`. If a change makes the
  README wrong, the change is not finished.

## Non-obvious invariants

Each of these looks like an oversight and is not. Read the linked commit before
changing any of them.

1. **`/var/mail` is deliberately not chowned.** `run` chowns `/var/lib/postfix`
   and `/var/spool/postfix` to `postfix`, and pointedly not `/var/mail` next to
   them. Adding it back breaks local delivery after the first restart:
   `local(8)` enforces `strict_mailbox_ownership` and refuses a mailbox not
   owned by the recipient. `local(8)` runs privileged and assumes the
   recipient's uid/gid itself, and `/var/mail` is already `root:mail 2775`
   setgid in the base image, so nothing needs chowning. Now pinned by
   `tests/test_lifecycle.py`, which asserts `root:mail` across a restart.
   (#104, commit `46ad15f`)

2. **Every `postconf -e` in `dkimConfig` and `srsConfig` is wrapped in an
   `if [ -z "$POSTFIX_..." ]`** — two in `dkimConfig`, four in `srsConfig`, six
   of six. These guards are the only reason a user can override
   `milter_default_action`, `smtpd_milters` and the four canonical-map settings
   through the documented `POSTFIX_*` variables. Removing one silently
   overwrites the user's value. (#134, commit `1748a61`; `tests/test_dkim.py`
   pins the milter half.)

3. **`postsrsd` is installed conditionally** — `if apt-cache show postsrsd`.
   Debian trixie does not build it for armhf, so an unconditional install fails
   the `linux/arm/v7` leg of **Build Image** with *"Unable to locate package
   postsrsd"*. The other halves of the decision are in `run` — setting
   `POSTSRSD_SRS_DOMAIN` on an image built without it exits 1 rather than
   relaying without the rewriting that was asked for — and in `healthcheck`,
   which fails once `postsrsd` was configured but is no longer running. All
   three move together. The refusal also has to stay *above* the postfix start:
   a container that came up first and died second would have relayed the
   unrewritten mail, and `tests/test_srs.py` asserts the log is empty at the
   point it exits. (`Closes #119`, commit `9755f7d`)

4. **The `Dockerfile` deletes `/etc/postsrsd.secret` at build time**, and `run`
   generates a random one when the file is missing *or empty* (`[ ! -s ]`, so a
   bind-mounted empty file is filled in rather than used as a zero-length key).
   The Debian package generates the secret in its `postinst`, so keeping it
   would ship *every* deployment with the same SRS key, letting any of them
   forge another's return addresses. The secret is `chmod a=,u=rw` and the
   README documents mounting it to keep return addresses valid for their 21
   days. (commit `9755f7d`)

5. **The `Dockerfile` also deletes `/etc/rsyslog.conf` at build time.** `run`
   only generates that file when it does not already exist — the "don't fiddle
   with a mounted config" branch. Shipping the packaged one would permanently
   take the generation path out of service and silently ignore
   `RSYSLOG_TIMESTAMP`, `RSYSLOG_LOG_TO_FILE` and the remote-forwarding
   variables.

6. **`HEALTHCHECK` is `/root/healthcheck`, a script, not `pgrep -x master`.**
   The README explains *why* the check covers every daemon; three details
   inside it are only in the code and are deliberate. Listening state is read
   from `/proc/net/tcp{,6}` with awk rather than by connecting, because a
   connect at the 30s interval would log a connect and a disconnect every
   interval. The service list comes from `postconf -M` and includes `maxproc 0`
   entries, because that is a process-count limit and not an off switch — a
   genuinely disabled service is removed with `postconf -MX` and stops being
   printed. And the opendkim and postsrsd conditions fall back to on-disk
   artefacts (`/etc/opendkim/KeyTable`, the marker in `/etc/default/postsrsd`)
   because a value read from a `<name>_FILE` exists only in the entrypoint's
   environment, not in the check's; `saslauthd` has no such artefact and is
   environment-only. `procps` is in the package list for the `pgrep` both
   scripts use. The `HEALTHCHECK` options are asserted by `tests/test_image.py`,
   and `--start-period=15s` is what keeps start-up out of the verdict.
   (commits `d22e380`, `e83e94a`)

7. **The SMTP greeting probe lives in `run`, once per container — not in the
   health check.** A running master is not a working relay: master binds the
   port and forks an smtpd per connection, so a setting smtpd rejects when it
   reads it leaves a container that listens, accepts and kills every session
   while `postconf -e`, `postfix check`, `/proc` and the health check all look
   fine. `run` therefore asks for one 220 and refuses to hand over without it.
   Three things about it are load-bearing: it runs *after* rsyslogd is started,
   so postfix's own `fatal:` line naming the setting is in the container log
   above the refusal; inside `awaitGreeting` the `2> /dev/null` is attached to
   the command substitution and not to the `exec`, because `exec … 2> /dev/null`
   has no command to apply to and would silence the script's own stderr for the
   rest of the container's life; and `smtpdPort` deliberately skips an smtpd
   bound to a single address, which may be there for something that does not
   answer this container. `healthcheck` does the opposite and checks every
   `inet` service including address-bound ones — both resolve a named endpoint
   such as `submission` through `getent services`. (issue #206, commit `1d6d8d3`)

8. **`run` has no `set -e`, and that is still load-bearing** — but not for the
   reason it once was. `dpkg-statoverride` is gone (see 10). What would break
   under errexit today is `smtpdPort=$(smtpdPort)`, where the function returns
   1 whenever `master.cf` has no all-interfaces smtpd and the next line treats
   an empty result as an expected state, and the `pkill -TERM saslauthd` /
   `pkill -TERM rsyslogd` pair in `stopDaemons`, where pkill exits 1 with
   nothing to match — the ordinary case for a container without `SASL_Passwds`
   — which would take the script down before it signals rsyslogd and waits for
   it. (`[ -n "$stopped" ] && return` is *not* such a case: bash's errexit
   ignores a failing command in an `&&` list other than the last.) Restarting a
   container re-runs the whole script over a filesystem that already has its
   results, which is also why `run` removes the four stale pid files near the
   top. Note that refusing to relay is now the normal response to a daemon that
   will not start or has died: `run` exits 1 from six places, and the SRS stop
   of invariant 3 is one of them, not the exception.

9. **`run` ends in a `pgrep -x` polling loop, not a bare `wait`.** rsyslogd is
   the only daemon that is a child of the script, so the bare `wait` this
   replaced watched one daemon out of five while the container went on relaying
   when any of the other four died. Polling needs nothing beyond reading
   `/proc` — signalling a daemon that dropped to its own user would need a
   capability the README asks deployments to drop. What is watched is the
   `supervised` list, appended to as each daemon is *confirmed* started, rather
   than the environment read a second time; rsyslogd is the exception, added as
   it is forked, because `wait -n` already watches it as the script's own job.
   Both sleeps run in the background and are `wait`ed (the first with `wait -n`),
   so a trapped SIGTERM is not postponed by a whole interval and rsyslogd's
   death is noticed at once. `stopDaemons` is guarded by `$stopped`, runs on
   both ways out, and stops rsyslogd last and waits for it so the others'
   parting words still reach the container log; a daemon that dies on its own
   exits 1 so an `on-failure` restart policy has something to act on.
   (issue #176, commit `cc23882`)

10. **The saslauthd mux directory is created with
    `install -d -o root -g sasl -m 710`, below the
    `chown -R postfix:postfix /var/spool/postfix`.** saslauthd makes its own mux
    world-writable, so the directory holding it is the access control.
    `dpkg-statoverride` only records what a *future* dpkg unpack should apply —
    nothing a container ever runs — so the mode never reached the directory at
    all, leaving an unrate-limited password oracle reachable by any uid, and
    re-adding the override was an error on every restart. Both the tool and the
    ordering matter. (issue #179, commit `259b2cb`)

11. **`mkdir -p /var/spool/postfix/dev` exists only for queues bind-mounted
    from the host.** The directory ships in the image, empty, and is where
    rsyslog opens the log socket the chrooted postfix daemons write to. Nothing
    else recreates it: it has no `postfix-files` entry, so `postfix check` does
    not, and Debian's chroot resync only fills `etc/`, `lib/` and
    `usr/lib/sasl2`. Without it everything that connects to syslog after
    chrooting is logged nowhere. (issue #180, commit `0697962`)

12. **In the generated `/etc/rsyslog.conf`, the `$template` /
    `$ActionFileDefaultTemplate` pair is written before the `/dev/stdout`
    action.** The directive applies only to actions written after it; with the
    other ordering `RSYSLOG_TIMESTAMP=no` stripped timestamps from
    `/var/log/mail.log` but not from the container log.

13. **The SRS block in `/etc/default/postsrsd` is delimited by `$srsMarker` and
    rebuilt on every start** (`sed -i "/$srsMarker/,\$d"`, then re-append). The
    file is *sourced* by the init script, so appending is what overrides the
    packaged defaults, and deleting from the marker to EOF first is what keeps
    a restart from stacking duplicate blocks — keep the marker text and the
    `sed` range in sync. The values are single-quoted, with embedded quotes
    closed/escaped/reopened, because that init script is `/bin/sh` with
    `set -e`: unquoted, a value containing a space is read as an assignment
    followed by a command, and the command not being found takes the init
    script down. Only the assignment is quoted, so the init script still splits
    `SRS_EXTRA_OPTIONS='-A -t60'` into two options. Two tests pin it — one
    sources the block with `sh` rather than matching text, one passes a value
    with a space. (issue #177, commit `fca55e7`)

14. **Only the envelope *sender* is rewritten by SRS**
    (`sender_canonical_classes=envelope_sender`) — extending that to headers
    would rewrite the visible `From:`, which is not what SRS is for. The
    recipient side is not symmetrical:
    `recipient_canonical_classes=envelope_recipient,header_recipient`, so
    recipient addresses in headers *are* rewritten, and have been since the
    feature landed. The comment above the block claims envelope-only for both;
    the code is what is described here.

15. **`echo -n > /etc/opendkim.conf` truncates the packaged config on every
    start**, and the loop that rebuilds it explicitly `continue`s past
    `OPENDKIM_DOMAINS` — that variable is this image's own input, not an
    OpenDKIM directive. Writing it in makes OpenDKIM reject the whole file with
    *"configuration error at line N: unrecognized parameter"*. It is not the
    only name that has to stay out: any `OPENDKIM_<name>_FILE` would land there
    too, and does not only because `secretsFromFiles` unsets the `_FILE`
    variable after reading it. That `unset` is part of this invariant.

16. **`dkimConfig` deletes `/etc/opendkim/KeyTable` and
    `/etc/opendkim/SigningTable` before rebuilding them**, because the loop
    below *appends* one line per domain and both files survive a restart.
    Without the two `rm -f`, every restart doubles every entry. This is the
    same rebuild-from-scratch decision as the SRS marker block in 13, without
    a comment in the code to say so.

17. **`secretsFromFiles` runs before every config loop, and uses `printf -v`.**
    It has to run first so the loops see the resolved value and not the `_FILE`
    variable; `printf -v "$var" '%s' "$(< "$file")"` is used because
    `$(< file)` drops trailing newlines, which a secret file is likely to end
    with and a password is unlikely to contain. An unreadable path exits 1
    rather than starting a relay without the credential. The mechanism covers
    exactly five prefixes — `POSTFIX_`, `POSTFIXMASTER_`, `POSTMAP_`,
    `OPENDKIM_` and `POSTSRSD_`. `SASL_Passwds`, `POSTMASTER_ADDRESS` and the
    `RSYSLOG_*` variables have no `_FILE` form, and `tests/test_secrets.py`
    names the same five.

18. **The POSTMAP loop is wrapped in `shopt -s nullglob` / `shopt -u nullglob`,
    and chowns the table and everything `postmap` generated from it to
    `root:root` mode 600.** A lookup table is a likely place for a password
    (`smtp_sasl_password_maps`), and postmap writes 644. Postfix opens its
    lookup tables in each daemon's pre-jail initialisation, while still root,
    so nothing needs to read them afterwards — this is the mode SASL_README
    asks for on this exact file. (issue #178; pinned by `tests/test_secrets.py`
    and `tests/test_sasl.py`)

19. **`dkimConfig` chowns and chmods `/etc/opendkim/keys` itself on every
    start, not just the key files.** That directory is a declared `VOLUME`, so
    its mode is whatever the volume driver left behind and not something the
    image ever set; opendkim walks the whole path down to a key, refuses one
    any other user could read or write, and names the *directory* in its error
    rather than the key.

20. **`/etc/postfix/sasl/smtpd.conf` and `/etc/pam.d/smtp` are written only
    when they do not already exist** (`[ ! -e ]`). Like the guards in 2, these
    look redundant and are the documented override: the README tells users to
    mount either file to replace the generated one. Writing them
    unconditionally silently discards a mounted configuration on every start.

21. **`SASL_Passwds` ships set-but-empty** (`SASL_Passwds=""` in the
    `Dockerfile`), and both `run` and `healthcheck` test it for *emptiness*,
    not existence. That asymmetry with the other feature switches is what keeps
    saslauthd from starting by default while the variable still shows up in
    `docker inspect`; `tests/test_image.py` pins it.

22. **The `POSTMASTER_ADDRESS` block sits before the generic `POSTFIX_*` loop
    and is unguarded**, so an explicit `POSTFIX_<class>_notice_recipient`
    simply overwrites it. `notify_classes` is deliberately left alone: setting
    bounce/2bounce/delay recipients is inert until someone widens it, and it
    just means they are already correct then.

23. **The default configuration is an open relay**
    (`POSTFIX_mynetworks=0.0.0.0/0`, `POSTFIX_smtpd_tls_security_level=none`)
    that relies on Docker networking for protection. This is the documented
    product decision, not a misconfiguration to harden — the README's
    *Securing the relay* section is the answer to it. Related and also
    deliberate: `POSTFIX_inet_protocols=ipv4` is pinned as a `Dockerfile`
    default, because Debian's postinst otherwise writes `inet_protocols` from
    whatever the *build machine* supported — the published image shipped `all`,
    which is half-configured when `mynetworks=0.0.0.0/0` covers no IPv6
    address. (commit `8e8e89c`)

24. **`${v//__/\/}` replaces every `__`, not the first.** The README promises
    "all double `__` symbols"; the code used to substitute only the first
    occurrence. Not reachable through documented usage, because a `postconf -M`
    service name holds at most one slash — the code was changed to match the
    documented rule rather than to rely on that. (commit `90839a0`)

25. **`perl` is named explicitly in the `Dockerfile` apt list.** It lands in
    the image anyway today, but only through opendkim-tools' `perl:any`
    dependency, which `perl-base` cannot satisfy — while `qshape`, a perl
    script shipped in the postfix package, is named in no postfix dependency at
    all. Naming perl adds no package and no bytes now, and keeps a correct
    narrowing of that opendkim dependency from taking `qshape` down on a
    routine base bump. `tests/test_qshape.py` exists for exactly this.
    (commit `2e420c4`)

26. **The image has no `ENTRYPOINT`, only `CMD ["/root/run"]`**, which is what
    makes `docker run --rm <image> mkpasswd …` work as the README documents,
    and nothing `chmod`s the two copied scripts — their executable bit comes
    from the git index (mode 100755) through `COPY`. Both are asserted by
    `tests/test_image.py`.

27. **`.dockerignore` excludes `tests` *and* `pytest.ini`,** while
    `tests/fixtures/postfix.py` builds the image from the repo root. That is
    consistent — neither is needed inside the image — but it means a fixture or
    pytest-config change never invalidates the build cache.

28. **`--dist loadfile` in `pytest.ini` is a requirement, not a preference.**
    `postfix_shared` pools one relay per configuration for the whole session;
    splitting a file across workers would start that relay once per worker and
    give up what the pooling buys. `--maxprocesses 4` is a measured cap, not a
    guess. The `smoke` marker is on exactly four tests in four different files,
    which is why the emulated arm/v7 job passes `-n0`: under `--dist loadfile`
    it would otherwise start four emulated relays at once against waits that
    were measured natively. (commit `ebbeb64`)

29. **`once_across_workers` in `tests/helpers.py` exists because xdist gives
    every worker its own session fixtures.** Without it every worker built the
    image and pulled the base at the same moment, and the registry answered
    429 rather than with the image. The first worker to create the lock does
    the work and touches a `.done` marker only after finishing; the rest wait
    on that marker. With no xdist worker in the environment it calls straight
    through.

30. **`POSTFIX_RELAY_IMAGE` is refused for an architecture the local daemon
    could build**, and `POSTFIX_RELAY_ARCH`, when set, must match what docker
    reports for the image. Building from the `Dockerfile` is what makes the
    suite test the tree it was run in; the escape hatch exists only for
    `arm/v7`, and an emulated job that meant to test another architecture and
    silently got the runner's own would pass everything and say nothing.
    (commit `5abb242`)

31. **Base-image bumping is Dependabot's, not `wader/bump`'s.** `Bumpfile`,
    `.github/workflows/bump.yml` and the `# bump:` directive on line 1 of the
    `Dockerfile` were all removed; line 1 is now a plain `FROM`. The two
    properties bump had are preserved by construction in
    `.github/dependabot.yml`: Dependabot only offers tags matching the shape of
    the current one, so `trixie-<date>-slim` stays on trixie and stays slim,
    and a date is not a semver minor or patch, so base-image PRs never match
    the auto-merge rule and always wait for review. A suite change
    (trixie → forky) is a deliberate edit, as it was before. (commit `5de83d0`)

### Comments that are currently wrong

Four comments in the tree contradict the code they describe. Do not take them
as evidence when auditing, and fix them where you are already editing the file:

- `.github/dependabot.yml` says `tests/requirements.txt` pins no versions and
  leaves out a pip ecosystem for that reason — it pins all six, so the premise
  for omitting the ecosystem no longer holds.
- `tests/test_image.py` still credits "bump" with keeping the base pin current
  (invariant 31).
- `tests/test_sasl.py` still describes the removed `dpkg-statoverride`
  mechanism in a docstring (invariant 10); the assertion it guards is correct.
- the comment above the SRS canonical maps in `run` claims envelope-only
  rewriting on both sides (invariant 14).

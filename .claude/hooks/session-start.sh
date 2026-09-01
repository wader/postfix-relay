#!/bin/bash

# SessionStart hook for Claude Code on the web.
#
# Everything that gates a pull request needs something this container does not
# have when a session starts: the Pytest jobs in .github/workflows/test.yml run
# a suite whose fixtures build and run containers through testcontainers, and
# Build Image in .github/workflows/ci.yml builds the image. So there are two
# jobs here, a running docker daemon and the dependencies from
# tests/requirements.txt.
#
# Deliberately best-effort, and deliberately without "set -e": a step that
# fails reports on stderr and the hook still exits 0, so an incomplete
# environment degrades the session instead of blocking it from starting.

# Do nothing outside a remote session. An unset variable, or "false", is a
# local checkout whose environment belongs to whoever set it up.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ] ; then
  exit 0
fi

repoDir="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
requirements="$repoDir/tests/requirements.txt"
dockerLog="/tmp/session-start-dockerd.log"

warn()
{
    echo "session-start: $*" >&2
}

# The image build and every test need a daemon, and the container starts
# without one. Idempotent: an already-running daemon is left alone, which is
# what a resumed or cleared session hits.
startDocker()
{
    if docker info > /dev/null 2>&1 ; then
      return 0
    fi

    if ! command -v dockerd > /dev/null 2>&1 ; then
      warn "no dockerd in this image: 'docker build' and the test suite cannot run"
      return 1
    fi

    nohup dockerd > "$dockerLog" 2>&1 &

    # Bounded wait, so a daemon that never comes up cannot hang start-up.
    for _ in $(seq 1 30) ; do
      if docker info > /dev/null 2>&1 ; then
        return 0
      fi
      sleep 1
    done

    warn "docker daemon did not come up within 30s, see $dockerLog"
    return 1
}

# Collection imports tests/conftest.py, every fixture module and every test
# module, so it fails exactly when a dependency is missing -- and it needs no
# daemon, because xdist does not distribute a collect-only run. The "cd" is
# required: tests is a package and its modules import each other by absolute
# name. This doubles as the idempotency check: a second run installs nothing.
testDepsReady()
{
    ( cd "$repoDir" && pytest --collect-only -q tests > /dev/null 2>&1 )
}

installTestDeps()
{
    if [ ! -f "$requirements" ] ; then
      warn "$requirements is missing, skipping test dependencies"
      return 1
    fi

    if testDepsReady ; then
      return 0
    fi

    # The image ships pytest as a uv-managed tool whose environment is isolated
    # from pip's site-packages, and its shim comes first on PATH. Installing
    # the requirements with pip alone therefore leaves the pytest that actually
    # runs without them -- and since pytest.ini's addopts pass xdist-only
    # flags, that pytest does not even reach collection: it dies on
    # "unrecognized arguments: -n". Going through uv keeps one pytest that can
    # see the whole requirements file.
    if command -v uv > /dev/null 2>&1 ; then
      uv tool install --force --with-requirements "$requirements" pytest > /dev/null 2>&1
      if testDepsReady ; then
        return 0
      fi
    fi

    # No uv, or uv did not resolve it: fall back to what the CI does.
    pip install --quiet --requirement "$requirements" > /dev/null 2>&1 ||
      pip install --quiet --break-system-packages --requirement "$requirements" > /dev/null 2>&1
    if testDepsReady ; then
      return 0
    fi

    warn "could not make 'pytest' import the test dependencies from $requirements"
    return 1
}

incomplete=no
startDocker || incomplete=yes
installTestDeps || incomplete=yes

if [ "$incomplete" == "yes" ] ; then
  warn "environment is incomplete: see the messages above before trusting a failing build or test run"
fi

exit 0

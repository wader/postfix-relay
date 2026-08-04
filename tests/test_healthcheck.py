import docker
import pytest
import time

from testcontainers.core.container import DockerContainer

HEALTHCHECK = "/root/healthcheck"
SUBMISSION = "submission/inet=submission inet n - y - - smtpd"

def start_relay(image, **env):
    container = DockerContainer(image=image)
    for name, value in env.items():
        container.with_env(name, value)

    container.start()
    # Postfix is the last daemon "run" starts, so a passing health check is
    # what "started" means here, and every test below breaks a relay that was
    # known to be up.
    run_healthcheck(container, expected=0, timeout=30)

    return container

def run_healthcheck(container, expected, timeout=10):
    # A killed daemon takes a moment to actually go away, and docker retries
    # the check anyway, so wait for the expected verdict instead of catching
    # the container mid-signal.
    deadline = time.monotonic() + timeout

    while True:
        exit_code, output = container.exec(HEALTHCHECK)
        if exit_code == expected or time.monotonic() > deadline:
            return exit_code, output.decode()
        time.sleep(0.5)

@pytest.fixture
def signing_relay(postfix_image):
    container = start_relay(postfix_image, OPENDKIM_DOMAINS='example.com')

    yield container
    container.stop()

def test_relay_with_everything_running_is_healthy(signing_relay):
    exit_code, _ = run_healthcheck(signing_relay, expected=0)

    assert exit_code == 0

def test_stopped_opendkim_is_unhealthy(signing_relay):
    signing_relay.exec("pkill -x opendkim")

    exit_code, output = run_healthcheck(signing_relay, expected=1)

    assert exit_code == 1
    assert 'relayed unsigned' in output

def test_relay_stops_when_a_daemon_exits(postfix_image):
    container = start_relay(postfix_image)
    wrapped = container.get_wrapped_container()

    try:
        container.exec("pkill -x rsyslogd")

        # Losing rsyslogd means relaying mail nobody can see afterwards, and
        # exiting 0 would look like a clean stop to a restart policy.
        assert wrapped.wait(timeout=30)['StatusCode'] == 1
        assert 'A daemon exited on its own' in wrapped.logs().decode()
    finally:
        container.stop()

def test_configured_but_unopened_port_is_unhealthy(postfix_image):
    # A submission port is a named service in master.cf rather than a number,
    # which the check has to resolve to know what to look for.
    container = start_relay(postfix_image)

    try:
        # master.cf gains the service but master is not reloaded, so postfix
        # keeps running while nothing answers on 587.
        container.exec(["postconf", "-M", "-e", SUBMISSION])

        exit_code, output = run_healthcheck(container, expected=1)
        assert exit_code == 1
        assert 'not listening on submission' in output

        container.exec("postfix reload")

        assert run_healthcheck(container, expected=0)[0] == 0
    finally:
        container.stop()

def test_daemon_that_cannot_start_stops_the_container(postfix_image):
    container = docker.from_env().containers.run(
        postfix_image,
        detach=True,
        environment={
            'OPENDKIM_DOMAINS': 'example.com',
            # Rejected by opendkim when it reads its configuration
            'OPENDKIM_NotARealSetting': '1',
        })

    try:
        assert container.wait(timeout=60)['StatusCode'] == 1
        assert 'opendkim did not start' in container.logs().decode()
    finally:
        container.remove(force=True)

def test_stopping_the_container_is_still_a_clean_exit(postfix_image):
    container = start_relay(postfix_image)
    wrapped = container.get_wrapped_container()

    try:
        wrapped.stop()

        assert wrapped.wait(timeout=30)['StatusCode'] == 0
    finally:
        container.stop()

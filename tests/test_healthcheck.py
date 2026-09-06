import docker
import pytest
import time

from testcontainers.core.container import DockerContainer

from tests.helpers import (container_log, exit_code_within,
                           healthcheck_after_stopping, wait_for_log)

HEALTHCHECK = "/root/healthcheck"
SUBMISSION = "submission/inet=submission inet n - y - - smtpd"
# The same service with its process count limit lifted, which postfix starts
# and binds exactly as it does the one above.
MAXPROC_ZERO = SUBMISSION.replace("n - y - -", "n - y - 0")

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
    exit_code, output = healthcheck_after_stopping(signing_relay, "opendkim")

    assert exit_code == 1
    assert 'relayed unsigned' in output.decode()

def test_relay_stops_when_a_daemon_exits(postfix_image):
    container = start_relay(postfix_image)
    wrapped = container.get_wrapped_container()

    try:
        container.exec("pkill -x rsyslogd")

        # Losing rsyslogd means relaying mail nobody can see afterwards, and
        # exiting 0 would look like a clean stop to a restart policy.
        assert exit_code_within(container, seconds=30) == 1
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
        # Not exit_code_within: this container comes straight from docker-py
        # rather than from the fixtures, and the helper unwraps a
        # testcontainers one.
        assert container.wait(timeout=60)['StatusCode'] == 1
        assert 'opendkim did not start' in container.logs().decode()
    finally:
        container.remove(force=True)

def test_stopping_the_container_is_still_a_clean_exit(postfix_image):
    container = start_relay(postfix_image)
    wrapped = container.get_wrapped_container()

    try:
        wrapped.stop()

        assert exit_code_within(container, seconds=30) == 0
    finally:
        container.stop()


@pytest.fixture
def relay_factory(postfix_image):
    """Relays for the tests that break them, stopped whatever happens."""
    started = []

    def start(**env):
        container = start_relay(postfix_image, **env)
        started.append(container)
        return container

    yield start

    for container in reversed(started):
        container.stop()


def test_a_relay_that_was_not_asked_to_sign_is_healthy_without_opendkim(relay_factory):
    """Only the daemons the environment asks for are required.

    A check that wanted opendkim everywhere would report every default
    deployment as broken.
    """
    relay = relay_factory()

    assert relay.exec(["pgrep", "-x", "opendkim"]).exit_code != 0
    assert run_healthcheck(relay, expected=0)[0] == 0


def test_a_relay_with_every_feature_turned_on_is_healthy(relay_factory):
    """Four daemons at once, which is the configuration with the most to
    go wrong and the one the check exists for."""
    relay = relay_factory(OPENDKIM_DOMAINS='example.com',
                          POSTSRSD_SRS_DOMAIN='srs.example.com',
                          SASL_Passwds='/etc/postfix/sasl/sasl_passwds')

    for daemon in ('master', 'rsyslogd', 'opendkim', 'postsrsd', 'saslauthd'):
        assert relay.exec(["pgrep", "-x", daemon]).exit_code == 0, daemon

    assert run_healthcheck(relay, expected=0)[0] == 0


def test_stopped_postsrsd_is_unhealthy(relay_factory):
    """A relay that lost it keeps sending, with the envelope senders that
    SRS was turned on to rewrite."""
    relay = relay_factory(POSTSRSD_SRS_DOMAIN='srs.example.com')

    exit_code, output = healthcheck_after_stopping(relay, "postsrsd")

    assert exit_code == 1
    assert 'envelope senders are not rewritten' in output.decode()


def test_stopped_saslauthd_is_unhealthy(relay_factory):
    """Nothing else notices: the relay stays up and simply refuses every
    client that tries to authenticate."""
    relay = relay_factory(SASL_Passwds='/etc/postfix/sasl/sasl_passwds')

    exit_code, output = healthcheck_after_stopping(relay, "saslauthd")

    assert exit_code == 1
    assert 'clients cannot authenticate' in output.decode()


def test_stopped_rsyslogd_is_unhealthy(relay_factory):
    """The one daemon every relay starts, and the only branch of the check
    that had no test.

    Nothing about the mail stops: postfix keeps accepting, signing and
    relaying, and the only difference is that none of it is written down --
    which is the failure an operator finds by looking for a delivery that
    left no line.
    """
    relay = relay_factory()

    exit_code, output = healthcheck_after_stopping(relay, "rsyslogd")

    assert exit_code == 1
    assert 'relayed unlogged' in output.decode()


def test_a_postfix_that_is_not_running_is_unhealthy(relay_factory):
    """The first thing the check looks at, and the one failure that means
    no mail is being accepted at all."""
    relay = relay_factory()

    # Stopped the way an operator would, but the master is supervised like
    # the rest, so this is the same race as the daemons above.
    exit_code, output = healthcheck_after_stopping(relay, "master",
                                                   stop="postfix stop")

    assert exit_code == 1
    assert 'postfix master is not running' in output.decode()


def test_a_service_with_no_process_limit_is_still_expected_to_listen(relay_factory):
    """"maxproc 0" means no process count limit, not "turned off".

    Postfix binds the port either way, so skipping those services would have
    left a submission port that never opened unnoticed. There is no field in
    master.cf that disables a service: one that really is disabled is removed
    with "postconf -MX", and postconf -M then does not print it at all.

    Written the way test_configured_but_unopened_port_is_unhealthy is, so
    that it fails if the exclusion is ever put back: the port is genuinely
    closed until the reload, which is the state an exclusion would hide.
    """
    relay = relay_factory()

    relay.exec(["postconf", "-M", "-e", MAXPROC_ZERO])

    exit_code, output = run_healthcheck(relay, expected=1)
    assert exit_code == 1
    assert 'not listening on submission' in output

    relay.exec("postfix reload")

    assert run_healthcheck(relay, expected=0)[0] == 0


def test_an_endpoint_given_as_a_number_is_checked(relay_factory):
    """A service may name a port instead of a service name, and may bind to
    one address, which the check has to read out of "host:port"."""
    relay = relay_factory()

    relay.exec(["postconf", "-M", "-e",
                "127.0.0.1:10025/inet=127.0.0.1:10025 inet n - y - - smtpd"])

    exit_code, output = run_healthcheck(relay, expected=1)
    assert exit_code == 1
    assert 'not listening on 127.0.0.1:10025' in output

    relay.exec("postfix reload")

    assert run_healthcheck(relay, expected=0)[0] == 0


def test_the_check_writes_nothing_to_the_log(relay_factory):
    """It runs every thirty seconds for the life of the container.

    Reading the listening sockets from the kernel rather than connecting to
    them is what keeps a connect and a disconnect out of the log each time.
    """
    relay = relay_factory()
    # Start-up is not over when the relay answers, which is the moment the
    # factory calls it started, and the snapshot has to be taken after the
    # last line it writes or that line arrives during the loop below and
    # reads as one the health check wrote. Two things land late: rsyslogd
    # writes its own start line once it has finished starting, and "run"
    # then asks smtpd for a greeting, which logs a connect and the disconnect
    # below. The disconnect is the last of them -- the probe sends QUIT
    # rather than dropping the connection precisely so that it is a
    # disconnect and not a lost connection.
    before = wait_for_log(relay, "disconnect from localhost")

    for _ in range(5):
        assert run_healthcheck(relay, expected=0)[0] == 0

    assert container_log(relay) == before

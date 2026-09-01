"""What the container has to be allowed to do.

The relay starts as root because postfix refuses to run as anything else, but
it hands the work to unprivileged users itself and needs very little of what
docker grants a container by default.
"""

import time

import pytest

from tests.helpers import container_exec, send

# The set documented in the README, and the reason for each of them:
# CHOWN and FOWNER for giving the queue to postfix and the DKIM keys to
# opendkim, DAC_OVERRIDE for postfix reading them back, SETGID and SETUID for
# the daemons dropping privileges, SYS_CHROOT for the jails postfix and
# postsrsd put them in, NET_BIND_SERVICE for port 25 wherever docker does not
# already allow it.
CAPABILITIES = ['CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'NET_BIND_SERVICE',
                'SETGID', 'SETUID', 'SYS_CHROOT']


@pytest.fixture
def hardened_relay(postfix_factory):
    """Relay with everything docker grants by default taken away but the above."""
    return postfix_factory(
        env={
            'OPENDKIM_DOMAINS': 'example.com',
            'POSTSRSD_SRS_DOMAIN': 'srs.example.com',
        },
        kwargs={
            'cap_drop': ['ALL'],
            'cap_add': CAPABILITIES,
            'security_opt': ['no-new-privileges'],
        })


def test_the_documented_capabilities_are_enough_to_relay(hardened_relay, mailpit):
    send(hardened_relay, sender='sender@example.com', subject='hardened')

    message = mailpit.wait_for_message('hardened')

    # Signing needs opendkim, which needs to drop privileges to its own user
    # and to read a key the start-up script gave it.
    assert 'dkim-signature' in message['headers']
    # Rewriting needs postsrsd, which runs in a chroot.
    assert message['ReturnPath'].startswith('SRS0=')


def test_the_health_check_passes_without_the_dropped_capabilities(hardened_relay):
    container_exec(hardened_relay, ["/root/healthcheck"])


def test_dropping_everything_stops_the_container(postfix_factory):
    """The set is what is needed, not a list nobody checked."""
    relay = postfix_factory(kwargs={'cap_drop': ['ALL']}, wait_ready=False)

    assert relay.get_wrapped_container().wait(timeout=60)['StatusCode'] == 1


def test_the_hardened_relay_stops_gracefully(hardened_relay):
    """Stopping is the operation most likely to miss a capability.

    "stopDaemons" signals daemons that dropped to their own users and waits
    for them, and the README says a graceful stop is one of the things the
    set above was checked against.
    """
    wrapped = hardened_relay.get_wrapped_container()

    started = time.monotonic()
    wrapped.stop(timeout=30)
    elapsed = time.monotonic() - started

    wrapped.reload()
    assert wrapped.attrs['State']['ExitCode'] == 0
    assert elapsed < 10, f"stopping took {elapsed:.1f}s, the SIGTERM trap did not run"

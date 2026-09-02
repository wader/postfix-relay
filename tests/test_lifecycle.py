"""Starting, restarting and queueing.

A relay is a long running container that gets restarted, updated and left
waiting for a server that is down, and has to keep the mail it accepted.
"""

import time

import pytest

from tests.helpers import (container_exec, container_log, container_stderr,
                           exit_code_within, poll_until, process_running, restart,
                           send, wait_for_file, wait_for_log, wait_for_smtp)


def health(container):
    wrapped = container.get_wrapped_container()
    wrapped.reload()
    return wrapped.attrs['State']['Health']['Status']


@pytest.mark.smoke
def test_the_container_reports_healthy(postfix):
    try:
        assert poll_until(lambda: health(postfix) == 'healthy', timeout=60,
                          description="the container to report healthy")
    except AssertionError:
        # Docker keeps what every probe printed and exited with, and a timeout
        # on its own says only that none of them passed. That is the whole
        # difference between "the check is too slow here" and "a daemon is
        # down", which is the question an emulated run exists to answer.
        wrapped = postfix.get_wrapped_container()
        wrapped.reload()
        for probe in wrapped.attrs['State']['Health'].get('Log', []):
            print(f"health probe exited {probe['ExitCode']}: {probe['Output']}")
        raise


def test_relaying_still_works_after_an_unclean_stop(postfix_factory, mailpit):
    """A killed container leaves pid files behind, which "run" cleans up.

    Postfix and rsyslogd both refuse to start when they believe an instance
    is already running, which is how a container ends up in a restart loop
    (issue #22).
    """
    relay = postfix_factory()

    send(relay, subject='before the kill')
    mailpit.wait_for_message('before the kill')

    # SIGKILL, so that nothing gets a chance to clean up after itself.
    relay.get_wrapped_container().kill()
    relay.get_wrapped_container().start()
    wait_for_smtp(relay)

    send(relay, subject='after the kill')

    assert mailpit.wait_for_message('after the kill')


def test_the_container_stops_gracefully(postfix_factory):
    """"docker stop" must not have to fall back to killing the container.

    The trap in "run" stops the services and lets the script return, so a stop
    takes a moment instead of the ten seconds docker waits before SIGKILL, and
    mail being handed over is not cut off half way.
    """
    relay = postfix_factory()
    wrapped = relay.get_wrapped_container()

    started = time.monotonic()
    wrapped.stop(timeout=30)
    elapsed = time.monotonic() - started

    wrapped.reload()
    assert wrapped.attrs['State']['ExitCode'] == 0
    assert elapsed < 10, f"stopping took {elapsed:.1f}s, the SIGTERM trap did not run"


def test_dkim_and_srs_come_back_after_an_unclean_stop(postfix_factory, mailpit):
    """The pid files opendkim and postsrsd leave behind are cleaned up too.

    Issue #22 was about postfix and rsyslogd, but a container killed with DKIM
    or SRS enabled has the same problem, and a relay that comes back up without
    them silently sends unsigned and unrewritten mail.
    """
    relay = postfix_factory(env={'OPENDKIM_DOMAINS': 'example.com=sel1',
                                 'POSTSRSD_SRS_DOMAIN': 'srs.example.com'})

    relay.get_wrapped_container().kill()
    relay.get_wrapped_container().start()
    wait_for_smtp(relay)

    assert process_running(relay, 'opendkim')
    assert process_running(relay, 'postsrsd')

    send(relay, sender='sender@example.com', subject='signed and rewritten again')

    message = mailpit.wait_for_message('signed and rewritten again')
    assert 'dkim-signature' in message['headers']
    assert message['ReturnPath'].startswith('SRS0=')


def test_local_mailboxes_are_not_taken_over(postfix_factory):
    """Mail for a local user is delivered and keeps its owner.

    postfix local(8) refuses to write a mailbox that is not owned by the
    recipient, so taking ownership of /var/mail on start-up broke local
    delivery after the first restart (issue #104).
    """
    relay = postfix_factory()

    send(relay, recipients=('root@localhost',), subject='local delivery')

    wait_for_file(relay, '/var/mail/root', 'Subject: local delivery')
    assert container_exec(relay, ["stat", "-c", "%U:%G", "/var/mail/root"]).strip() == 'root:mail'

    restart(relay)

    send(relay, recipients=('root@localhost',), subject='local delivery after restart')

    wait_for_file(relay, '/var/mail/root', 'Subject: local delivery after restart')
    assert container_exec(relay, ["stat", "-c", "%U:%G", "/var/mail/root"]).strip() == 'root:mail'


def test_mail_waits_in_the_queue_until_the_relayhost_answers(postfix_factory,
                                                             mailpit_factory):
    """Accepted mail is queued, not lost, while the next hop is unreachable."""
    relay = postfix_factory(env={'POSTFIX_relayhost': 'late-mailpit:1025'})

    send(relay, subject='deferred')

    wait_for_log(relay, 'status=deferred')

    queue = container_exec(relay, ["postqueue", "-p"])
    assert 'receiver@example.com' in queue
    assert 'Host or domain name not found' in queue

    # And a restart does not lose it: /var/spool/postfix is a volume for
    # exactly this, mail that was accepted has to be delivered eventually.
    restart(relay)
    assert 'receiver@example.com' in container_exec(relay, ["postqueue", "-p"])

    late = mailpit_factory('late-mailpit')
    container_exec(relay, ["postqueue", "-f"])

    assert late.wait_for_message('deferred')


def test_the_start_up_script_is_the_containers_first_process(postfix):
    """It is what receives the SIGTERM docker sends, and its trap is what
    turns that into a clean stop of every daemon."""
    assert 'run' in container_exec(postfix, ["cat", "/proc/1/cmdline"])


def test_the_queue_survives_replacing_the_container(docker_volume, postfix_factory,
                                                    mailpit_factory):
    """The reason /var/spool/postfix is a volume, and what the README says
    to mount it for.

    Mail that was accepted has been promised to the sender, so a container
    that is replaced while the next hop is down must hand the queue over to
    its replacement rather than lose it.
    """
    env = {'POSTFIX_relayhost': 'replacement-target:1025'}
    volumes = {docker_volume: '/var/spool/postfix'}

    first = postfix_factory(env=env, volumes=volumes)
    send(first, subject='queued across containers')
    wait_for_log(first, 'status=deferred')
    first.get_wrapped_container().stop()

    second = postfix_factory(env=env, volumes=volumes)

    assert 'receiver@example.com' in container_exec(second, ["postqueue", "-p"])

    late = mailpit_factory('replacement-target')
    container_exec(second, ["postqueue", "-f"])

    assert late.wait_for_message('queued across containers')


def test_the_relay_comes_back_from_several_restarts(postfix_factory, mailpit):
    """Nothing in the start-up is one-shot.

    Every restart runs the whole script again over a filesystem it already
    wrote, which is where an unrepeatable step would show up.
    """
    relay = postfix_factory(env={'OPENDKIM_DOMAINS': 'example.com=sel1',
                                 'POSTSRSD_SRS_DOMAIN': 'srs.example.com'})

    for attempt in range(3):
        restart(relay)

        send(relay, sender='sender@example.com', subject=f'restart {attempt}')
        message = mailpit.wait_for_message(f'restart {attempt}')

        assert 'dkim-signature' in message['headers']
        assert message['ReturnPath'].startswith('SRS0=')


def test_an_interrupt_stops_the_container_cleanly(postfix_factory):
    """The trap catches SIGINT as well as SIGTERM, which is what a relay
    started in the foreground with "docker run" is stopped with."""
    relay = postfix_factory()
    wrapped = relay.get_wrapped_container()

    wrapped.kill(signal='SIGINT')

    assert exit_code_within(relay, seconds=30) == 0


# Every daemon "run" starts, and what turns it on. rsyslogd is the only one it
# starts in the foreground; the others are supervised by polling, so all five
# have to take the container down with them.
SUPERVISED_DAEMONS = [
    ('rsyslogd', {}),
    ('master', {}),
    ('opendkim', {'OPENDKIM_DOMAINS': 'example.com'}),
    ('postsrsd', {'POSTSRSD_SRS_DOMAIN': 'srs.example.com'}),
    ('saslauthd', {'SASL_Passwds': '/etc/postfix/sasl/sasl_passwds'}),
]


@pytest.mark.parametrize("daemon,env", SUPERVISED_DAEMONS,
                         ids=[daemon for daemon, _ in SUPERVISED_DAEMONS])
def test_the_container_stops_when_a_daemon_it_started_exits(daemon, env, postfix_factory):
    """The README's promise, for each of the five daemons in turn.

    "A daemon that later gives up on its own exits the container non-zero,
    so restart: on-failure brings it back". A relay that keeps running
    without one of them keeps accepting mail: unsigned, unrewritten,
    unlogged, or -- when it is the postfix master that is gone -- not at
    all, with nothing but the health check to say so.
    """
    relay = postfix_factory(env=env)

    relay.exec(f"pkill -x {daemon}")

    # Longer than the default bound: rsyslogd is a job of the start-up script
    # and its death is seen at once, but the other four are noticed by the
    # supervision loop, so the wait has to cover a poll interval and the
    # second reading that confirms it. It costs nothing when the container
    # does stop, which is the outcome being asserted.
    assert exit_code_within(relay, seconds=15) == 1


def test_a_setting_postfix_cannot_serve_with_stops_the_container(postfix_factory):
    """A relay that listens and kills every session is worse than one that
    is down: nothing about it looks wrong from outside.

    An empty error_notice_recipient is the reachable case -- the README
    documents an empty value as the way to clear a default, postconf takes
    it without a word and "postfix check" passes, but smtpd reads it at the
    start of every session and dies on it. The master keeps the port open
    throughout, so the health check has a running master and a listening
    socket to look at and reports healthy (issue #206).
    """
    relay = postfix_factory(env={'POSTFIX_error_notice_recipient': ''},
                            wait_ready=False)

    # Longer than the daemon cases above: master holds the connection open
    # with nothing behind it while it throttles the smtpd it could not start,
    # so each of the five attempts waits out its own read timeout.
    assert exit_code_within(relay, seconds=25) == 1

    log = container_log(relay) + container_stderr(relay)
    # Postfix's own reason, which names the setting, and then this image's.
    assert 'bad string length' in log
    assert 'No smtpd survived a connection' in log


def test_the_chroot_still_works_when_the_queue_is_mounted_from_the_host(
        tmp_path, postfix_factory):
    """The README's own volume example is a host directory, which is empty.

    Postfix runs its daemons chrooted in the queue directory and rsyslog
    puts a second log socket in the /dev inside it. Mounting over the queue
    hides that directory, rsyslogd reports it cannot create the socket, and
    the daemons that connect to syslog after chrooting are logged nowhere.
    """
    queue = tmp_path / "spool"
    queue.mkdir()

    relay = postfix_factory(volumes={str(queue): '/var/spool/postfix'})

    assert relay.exec(["test", "-S", "/var/spool/postfix/dev/log"]).exit_code == 0
    assert 'cannot create' not in container_log(relay)


def test_the_greeting_check_still_covers_a_customised_smtp_service(postfix_factory):
    """A master.cf entry carries its options after the command.

    Adding "-o" options to smtp/inet is what POSTFIXMASTER_ is documented for,
    and it moves the command away from the end of the line. Reading the last
    field to find the smtpd to greet matches nothing then, no probe runs, and
    the check above passes by not happening -- on the relays that were
    customised, which are the ones most likely to have been customised wrong.
    """
    relay = postfix_factory(
        env={
            'POSTFIX_error_notice_recipient': '',
            'POSTFIXMASTER_smtp__inet':
                'smtp inet n - y - - smtpd -o smtpd_client_restrictions=permit_mynetworks,reject',
        },
        wait_ready=False)

    assert exit_code_within(relay, seconds=25) == 1
    assert 'No smtpd survived a connection' in container_log(relay) + container_stderr(relay)


def test_a_relay_that_does_not_serve_loopback_still_starts(postfix_factory, mailpit):
    """inet_interfaces names the addresses the relay serves, loopback or not.

    Greeting 127.0.0.1 regardless refuses to start a relay that is working
    for every client it has, and tells its operator to look for a postfix
    "fatal:" line that was never written.
    """
    relay = postfix_factory(env={'POSTFIX_inet_interfaces': 'relay-off-loopback'},
                            kwargs={'hostname': 'relay-off-loopback'})

    # The probe is the last thing start-up does, and it takes its five
    # attempts before giving up: a connection accepted through the published
    # port only says the master is up, so the relay is given the time to
    # refuse before it is called started.
    assert exit_code_within(relay, seconds=20) is None

    send(relay, subject='off loopback')

    assert mailpit.wait_for_message('off loopback')


def test_a_relay_that_names_its_interfaces_with_a_macro_still_starts(postfix_factory,
                                                                     mailpit):
    """inet_interfaces is often written "$myhostname" rather than spelled out.

    postconf hands back what main.cf holds unless it is asked to expand, so
    reading the setting without that gives the literal "$myhostname" to dial:
    five refused connections and a container that exits 1 for a relay serving
    every client it has.
    """
    relay = postfix_factory(env={'POSTFIX_myhostname': 'relay-by-macro',
                                 'POSTFIX_inet_interfaces': '$myhostname'},
                            kwargs={'hostname': 'relay-by-macro'})

    assert exit_code_within(relay, seconds=20) is None

    send(relay, subject='named by macro')

    assert mailpit.wait_for_message('named by macro')


def test_the_stop_path_does_not_claim_daemons_it_never_signalled(postfix_factory):
    """Two of the three init scripts cannot find their daemon in a container.

    opendkim's and postsrsd's both identify the process with
    start-stop-daemon's --exec, which resolves /proc/<pid>/exe, and both
    daemons have dropped to their own user by then: reading that link across a
    uid boundary needs CAP_SYS_PTRACE, which docker does not grant by default
    and the capability list in the README does not ask for. Neither daemon was
    signalled at all -- opendkim's script said "none killed" and postsrsd's,
    which passes --oknodo, reported success it had not had.

    Those two lines are what the mistake looks like from outside, and their
    absence is what this pins: a shutdown that says nothing about a daemon it
    did not stop, because it stops it.
    """
    relay = postfix_factory(env={'OPENDKIM_DOMAINS': 'example.com',
                                 'POSTSRSD_SRS_DOMAIN': 'srs.example.com'})

    poll_until(lambda: process_running(relay, 'postsrsd'),
               description="postsrsd to be running before the container is stopped")

    relay.get_wrapped_container().stop(timeout=30)

    log = container_log(relay) + container_stderr(relay)

    assert 'none killed' not in log
    assert 'Stopping Postfix Sender Rewriting Scheme daemon' not in log


def test_a_stop_during_start_up_is_not_reported_as_a_failure(postfix_factory):
    """SIGTERM before the relay is up is still a stop the operator asked for.

    Generating several keys holds start-up open long enough for the signal to
    land in it. Should it land after start-up instead, this passes on the
    ordinary path rather than failing: what it must never do is report a
    daemon that would not start.
    """
    relay = postfix_factory(
        env={'OPENDKIM_DOMAINS': ' '.join(f"d{n}.example" for n in range(8))},
        wait_ready=False)
    wrapped = relay.get_wrapped_container()

    # Waited for rather than slept through. "DNS records:" is printed before
    # the first key is generated and start-up runs for several seconds after
    # it, so the signal lands inside start-up because the log says start-up is
    # running -- not because a second happened to be the right guess on the
    # machine it was written on.
    wait_for_log(relay, "DNS records:")
    wrapped.stop(timeout=30)

    assert exit_code_within(relay, seconds=30) == 0
    assert 'did not start' not in container_stderr(relay)

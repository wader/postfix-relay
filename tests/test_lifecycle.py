"""Starting, restarting and queueing.

A relay is a long running container that gets restarted, updated and left
waiting for a server that is down, and has to keep the mail it accepted.
"""

import time

from tests.helpers import (container_exec, poll_until, process_running, restart, send,
                           wait_for_file, wait_for_log, wait_for_smtp)


def health(container):
    wrapped = container.get_wrapped_container()
    wrapped.reload()
    return wrapped.attrs['State']['Health']['Status']


def test_the_container_reports_healthy(postfix):
    assert poll_until(lambda: health(postfix) == 'healthy', timeout=60,
                      description="the container to report healthy")


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

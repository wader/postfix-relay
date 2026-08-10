"""What the container logs and where.

Everything goes through rsyslog, configured from RSYSLOG_ variables, and
the container log is the only place a user gets postfix messages from
unless they ask for more (issue #58).
"""

import re

from tests.helpers import (container_exec, container_log, restart, send,
                           wait_for_file, wait_for_log)

MAIL_LOG_LINE = re.compile(r'^postfix/\w+\[\d+\]: ')
TIMESTAMPED_MAIL_LOG_LINE = re.compile(r'^\d{4}-\d{2}-\d{2}T[\d:.+]+ \S+ postfix/\w+\[\d+\]: ')


def postfix_log_lines(container):
    return [line for line in container_log(container).splitlines() if 'postfix/' in line]


def test_container_log_has_no_timestamps_by_default(postfix, mailpit, smtp):
    """RSYSLOG_TIMESTAMP defaults to "no".

    Docker timestamps the lines it collects itself, so repeating the time in
    every message only makes the log harder to read.
    """
    smtp.sendmail('sender@example.com', ['receiver@example.com'],
                  'Subject: no timestamps\r\n\r\nbody\r\n')
    mailpit.wait_for_message('no timestamps')

    lines = postfix_log_lines(postfix)

    assert lines
    assert all(MAIL_LOG_LINE.match(line) for line in lines), lines[:3]


def test_timestamps_can_be_asked_for(postfix_factory, mailpit):
    relay = postfix_factory(env={'RSYSLOG_TIMESTAMP': 'yes'})

    send(relay, subject='with timestamps')
    mailpit.wait_for_message('with timestamps')

    lines = postfix_log_lines(relay)

    assert lines
    assert all(TIMESTAMPED_MAIL_LOG_LINE.match(line) for line in lines), lines[:3]


def test_mail_can_also_be_logged_to_a_file(postfix_factory, mailpit):
    relay = postfix_factory(env={'RSYSLOG_LOG_TO_FILE': 'yes'})

    send(relay, subject='logged to file')
    mailpit.wait_for_message('logged to file')

    assert wait_for_file(relay, '/var/log/mail.log', 'status=sent')


def test_rsyslog_d_configuration_is_included(postfix_factory, mailpit):
    """Dropping a .conf file in /etc/rsyslog.d extends the configuration."""
    relay = postfix_factory(
        files={'/etc/rsyslog.d/99-test.conf': 'mail.* /var/log/from-include.log\n'})

    send(relay, subject='logged by an include')
    mailpit.wait_for_message('logged by an include')

    assert wait_for_file(relay, '/var/log/from-include.log', 'status=sent')


def test_an_existing_rsyslog_conf_is_left_alone(postfix_factory, mailpit):
    """A mounted /etc/rsyslog.conf replaces the generated one.

    Which also means the RSYSLOG_ variables stop doing anything, since the
    whole block that reads them is skipped.
    """
    config = ('$ModLoad imuxsock\n'
              '$WorkDirectory /var/spool/rsyslog\n'
              '*.* /var/log/mounted.log\n')

    relay = postfix_factory(files={'/etc/rsyslog.conf': config},
                            env={'RSYSLOG_LOG_TO_FILE': 'yes'})

    assert 'Skipping /etc/rsyslog.conf generating' in container_log(relay)
    assert container_exec(relay, ["cat", "/etc/rsyslog.conf"]) == config
    assert relay.exec(["test", "-e", "/var/log/mail.log"]).exit_code != 0

    send(relay, subject='logged as configured')
    mailpit.wait_for_message('logged as configured')

    assert wait_for_file(relay, '/var/log/mounted.log', 'status=sent')
    # Nothing was sent to stdout, the mounted configuration does not ask for it.
    assert postfix_log_lines(relay) == []


def test_messages_are_forwarded_to_a_remote_syslog_server(postfix_factory, mailpit):
    """RSYSLOG_REMOTE_HOST forwards everything, over UDP and port 514 by default.

    The receiver is the image itself with an /etc/rsyslog.d file turning on
    imudp, so the forwarding is checked by reading what actually arrived rather
    than by reading back the configuration that was written.
    """
    receiver = postfix_factory(alias='syslog-receiver', files={
        '/etc/rsyslog.d/10-receiver.conf': ('module(load="imudp")\n'
                                            'input(type="imudp" port="514")\n'
                                            '*.* /var/log/received.log\n')})
    sender = postfix_factory(env={'RSYSLOG_REMOTE_HOST': 'syslog-receiver'})

    send(sender, subject='forwarded')
    mailpit.wait_for_message('forwarded')

    received = wait_for_file(receiver, '/var/log/received.log', 'status=sent')
    # The forwarded lines carry the sending container's hostname, so this is
    # the sender's log and not the receiver's own.
    assert sender.get_wrapped_container().id[:12] in received


def test_the_remote_port_and_template_can_be_changed(postfix_factory):
    relay = postfix_factory(env={
        'RSYSLOG_REMOTE_HOST': 'syslog.example',
        'RSYSLOG_REMOTE_PORT': '5514',
        'RSYSLOG_REMOTE_TEMPLATE': 'RSYSLOG_SyslogProtocol23Format',
    })

    assert 'target="syslog.example" port="5514" ' \
           'template="RSYSLOG_SyslogProtocol23Format"' in \
        container_exec(relay, ["cat", "/etc/rsyslog.conf"])


def test_the_timezone_is_used_in_log_timestamps(postfix_factory, mailpit):
    """TZ is handled by the base image, and the README says so."""
    relay = postfix_factory(env={'TZ': 'Europe/Prague', 'RSYSLOG_TIMESTAMP': 'yes'})

    send(relay, subject='in local time')
    mailpit.wait_for_message('in local time')

    lines = postfix_log_lines(relay)

    assert lines
    # Prague is one or two hours ahead of UTC, depending on the season.
    assert all(re.search(r'T[\d:.]+\+0[12]:00 ', line) for line in lines), lines[:3]


def test_authentication_messages_are_kept_off_stdout(postfix_shared):
    """The generated configuration is "*.*;auth,authpriv.none /dev/stdout".

    saslauthd and PAM write to the auth facility, which is where a password
    can end up in a log line, so those two facilities are the ones docker
    logs does not collect. Everything else has to reach it.
    """
    relay = postfix_shared(env={'RSYSLOG_LOG_TO_FILE': 'no'})

    container_exec(relay, ["logger", "-p", "auth.info", "-t", "probe", "a login attempt"])
    container_exec(relay, ["logger", "-p", "authpriv.info", "-t", "probe", "a password"])
    container_exec(relay, ["logger", "-p", "mail.info", "-t", "probe", "a delivery"])

    log = wait_for_log(relay, 'a delivery')

    assert 'a login attempt' not in log
    assert 'a password' not in log


def test_every_line_names_the_daemon_that_wrote_it(postfix, mailpit, smtp):
    """A relay writes as five or six different programs at once, and the
    tag is the only thing that says which."""
    smtp.sendmail('sender@example.com', ['receiver@example.com'],
                  'Subject: tagged\r\n\r\nbody\r\n')
    mailpit.wait_for_message('tagged')

    tags = {line.split('[')[0] for line in postfix_log_lines(postfix)}

    assert 'postfix/smtpd' in tags
    assert 'postfix/smtp' in tags
    assert 'postfix/qmgr' in tags


def test_a_file_log_is_written_beside_the_container_log_and_not_instead_of_it(
        postfix_factory, mailpit):
    """RSYSLOG_LOG_TO_FILE adds a destination.

    A user who mounts /var/log to keep a history must not lose "docker logs"
    for it, which is where every other tool looks.
    """
    relay = postfix_factory(env={'RSYSLOG_LOG_TO_FILE': 'yes'})

    send(relay, subject='in both places')
    mailpit.wait_for_message('in both places')

    assert wait_for_file(relay, '/var/log/mail.log', 'status=sent')
    assert postfix_log_lines(relay)


def test_the_timestamp_setting_applies_to_the_file_log_too(postfix_factory, mailpit):
    """The template is set once, before both destinations are written, so
    asking for no timestamps means none anywhere -- worth knowing before
    mounting the file somewhere that has no timestamps of its own."""
    relay = postfix_factory(env={'RSYSLOG_LOG_TO_FILE': 'yes'})

    send(relay, subject='no timestamps in the file either')
    mailpit.wait_for_message('no timestamps in the file either')

    logged = wait_for_file(relay, '/var/log/mail.log', 'status=sent')
    lines = [line for line in logged.splitlines() if 'postfix/' in line]

    assert lines
    assert all(MAIL_LOG_LINE.match(line) for line in lines), lines[:3]


def test_forwarding_defaults_to_udp_on_the_standard_syslog_port(postfix_shared):
    """RSYSLOG_REMOTE_HOST on its own is the documented minimum."""
    configuration = container_exec(
        postfix_shared(env={'RSYSLOG_REMOTE_HOST': 'syslog.example'}),
        ["cat", "/etc/rsyslog.conf"])

    assert 'action(type="omfwd" target="syslog.example" port="514" ' \
           'template="RSYSLOG_ForwardFormat")' in configuration


def test_the_configuration_is_generated_once_and_kept_across_restarts(
        postfix_factory, mailpit):
    """It is written to the container's own filesystem, so the second start
    finds it and says so instead of writing it again."""
    relay = postfix_factory(env={'RSYSLOG_TIMESTAMP': 'yes'})
    generated = container_exec(relay, ["cat", "/etc/rsyslog.conf"])

    restart(relay)

    assert container_exec(relay, ["cat", "/etc/rsyslog.conf"]) == generated
    assert 'Skipping /etc/rsyslog.conf generating' in container_log(relay)

    send(relay, subject='logged after a restart')
    mailpit.wait_for_message('logged after a restart')

    # Only what the second start wrote: the init script prints its own
    # progress without a trailing newline, so the line that reports the
    # stop and the first line of the restarted rsyslogd share one line.
    restarted = container_log(relay).split('Skipping /etc/rsyslog.conf generating')[-1]
    lines = [line for line in restarted.splitlines() if 'postfix/' in line]

    assert lines
    assert all(TIMESTAMPED_MAIL_LOG_LINE.match(line) for line in lines), lines[:3]

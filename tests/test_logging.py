"""What the container logs and where.

Everything goes through rsyslog, configured from RSYSLOG_ variables, and
the container log is the only place a user gets postfix messages from
unless they ask for more (issue #58).
"""

import re

from tests.helpers import container_exec, container_log, send, wait_for_file

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
    """A mounted /etc/rsyslog.conf replaces the generated one."""
    config = ('$ModLoad imuxsock\n'
              '$WorkDirectory /var/spool/rsyslog\n'
              '*.* /var/log/mounted.log\n')

    relay = postfix_factory(files={'/etc/rsyslog.conf': config})

    assert 'Skipping /etc/rsyslog.conf generating' in container_log(relay)
    assert container_exec(relay, ["cat", "/etc/rsyslog.conf"]) == config

    send(relay, subject='logged as configured')
    mailpit.wait_for_message('logged as configured')

    assert wait_for_file(relay, '/var/log/mounted.log', 'status=sent')
    # Nothing was sent to stdout, the mounted configuration does not ask for it.
    assert postfix_log_lines(relay) == []

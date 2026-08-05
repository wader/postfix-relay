"""Turning environment variables into postfix configuration.

The image is configured through POSTFIX_, POSTFIXMASTER_ and POSTMAP_
variables, so the mapping from a variable to what postfix ends up doing is
the part of "run" users depend on the most.
"""

import smtplib

import pytest

from tests.helpers import container_exec, esmtp_features, postconf, send, smtp_connect


def test_postfix_variables_configure_main_cf(postfix_factory, mailpit):
    relay = postfix_factory(env={
        'POSTFIX_message_size_limit': '2000',
        'POSTFIX_smtpd_recipient_restrictions': 'permit_mynetworks, reject_unauth_destination',
    })

    assert postconf(relay, 'message_size_limit') == '2000'
    # Values with spaces and commas have to survive the round trip.
    assert postconf(relay, 'smtpd_recipient_restrictions') == \
        'permit_mynetworks, reject_unauth_destination'

    # And the setting is not just stored, postfix enforces it.
    with pytest.raises(smtplib.SMTPSenderRefused) as rejected:
        send(relay, subject='too big', body='x' * 5000)
    assert rejected.value.smtp_code == 552

    send(relay, subject='small enough')
    assert mailpit.wait_for_message('small enough')


def test_myhostname_is_what_the_relay_calls_itself(postfix_factory, mailpit):
    """The one setting the README tells every user to set."""
    relay = postfix_factory(env={
        'POSTFIX_myhostname': 'smtp.example.test',
        # An empty value clears a Dockerfile default instead of being skipped,
        # which is the only way to turn one of them off.
        'POSTFIX_smtp_tls_security_level': '',
    })

    code, banner, _ = esmtp_features(relay)
    assert code == 220
    assert banner.startswith('smtp.example.test ESMTP')
    assert postconf(relay, 'smtp_tls_security_level') == ''

    send(relay, subject='named relay')

    assert any('by smtp.example.test (Postfix)' in received
               for received in mailpit.wait_for_message('named relay')['headers']['received'])


def test_postfixmaster_variables_configure_master_cf(postfix_factory, mailpit):
    """A POSTFIXMASTER_ variable adds a service, "__" standing for "/"."""
    relay = postfix_factory(
        env={'POSTFIXMASTER_submission__inet': 'submission inet n - y - - smtpd'},
        ports=(25, 587),
    )

    assert 'submission inet' in container_exec(relay, ["postconf", "-M", "submission/inet"])

    send(relay, port=587, subject='through submission')

    assert mailpit.wait_for_message('through submission')


def test_postmap_variables_create_indexed_tables(postfix_factory, mailpit):
    """POSTMAP_<file> writes /etc/postfix/<file> and runs postmap on it."""
    table = ("routed.example relay:[mailpit]:1025\n"
             "other.example relay:[mailpit]:1025")

    relay = postfix_factory(env={
        # Without a relayhost, only the transport map can route this mail.
        'POSTFIX_relayhost': '',
        'POSTFIX_transport_maps': 'hash:/etc/postfix/transport',
        'POSTMAP_transport': table,
    })

    assert container_exec(relay, ["cat", "/etc/postfix/transport"]).strip() == table
    assert container_exec(
        relay, ["postmap", "-q", "routed.example", "hash:/etc/postfix/transport"]
    ).strip() == "relay:[mailpit]:1025"

    send(relay, recipients=('receiver@routed.example',), subject='transported')

    assert mailpit.wait_for_message('transported')


def test_mynetworks_restricts_who_may_relay(postfix_factory, mailpit):
    """The default open relay can be closed down with POSTFIX_mynetworks.

    The image is an open relay by default and only docker networking keeps
    it safe, which is what issues #16, #20 and #107 are all about.
    """
    relay = postfix_factory(env={'POSTFIX_mynetworks': '127.0.0.0/8'})

    with smtp_connect(relay) as smtp:
        with pytest.raises(smtplib.SMTPRecipientsRefused) as rejected:
            smtp.sendmail('sender@example.com', ['receiver@example.com'],
                          'Subject: denied\r\n\r\nbody\r\n')

    code, message = rejected.value.recipients['receiver@example.com']
    assert code == 454
    assert b'Relay access denied' in message

    mailpit.assert_nothing_delivered()

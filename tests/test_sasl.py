"""SMTP authentication, on both sides of the relay.

Clients authenticating to the relay (issues #69 and #79) and the relay
authenticating to the server it hands the mail over to, which is what
relaying through a provider such as gmail or sendgrid needs.
"""

import smtplib

import docker
import pytest

from testcontainers.community.mailpit import MailpitUser

from tests.helpers import send, smtp_connect, wait_for_log

PASSWD_FILE = '/etc/postfix/sasl/sasl_passwds'
USER, PASSWORD = 'myuser', 'mypassword'


def mkpasswd(image, password):
    """Hash a password the way the README tells users to."""
    output = docker.from_env().containers.run(
        image, ["mkpasswd", "-m", "sha-512", password], remove=True)
    return output.decode().strip()


@pytest.fixture
def authenticated_relay(postfix_image, postfix_factory):
    """Relay set up like the "Example Basic Client PAM Auth" README section."""
    return postfix_factory(
        env={
            'SASL_Passwds': PASSWD_FILE,
            'POSTFIX_smtpd_sasl_auth_enable': 'yes',
            'POSTFIX_cyrus_sasl_config_path': '/etc/postfix/sasl',
            'POSTFIX_smtpd_sasl_security_options': 'noanonymous',
            'POSTFIX_smtpd_relay_restrictions': 'permit_sasl_authenticated,reject',
        },
        files={PASSWD_FILE: f"{USER}:{mkpasswd(postfix_image, PASSWORD)}\n"},
    )


def test_authenticated_clients_may_relay(authenticated_relay, mailpit):
    for mechanism in ['PLAIN', 'LOGIN']:
        with smtp_connect(authenticated_relay) as smtp:
            smtp.ehlo()
            assert mechanism in smtp.esmtp_features['auth']

            # Pick the mechanism explicitly instead of letting smtplib
            # choose: it would start with CRAM-MD5, which a saslauthd
            # pwcheck_method cannot support.
            smtp.user, smtp.password = USER, PASSWORD
            code, _ = smtp.auth(mechanism, getattr(smtp, f"auth_{mechanism.lower()}"))
            assert code == 235

            smtp.sendmail('sender@example.com', ['receiver@example.com'],
                          f'Subject: authenticated with {mechanism}\r\n\r\nbody\r\n')

        assert mailpit.wait_for_message(f'authenticated with {mechanism}')


def test_clients_without_valid_credentials_may_not_relay(authenticated_relay, mailpit):
    with smtp_connect(authenticated_relay) as smtp:
        with pytest.raises(smtplib.SMTPRecipientsRefused) as rejected:
            smtp.sendmail('sender@example.com', ['receiver@example.com'],
                          'Subject: not authenticated\r\n\r\nbody\r\n')

    code, _ = rejected.value.recipients['receiver@example.com']
    assert code == 554

    with smtp_connect(authenticated_relay) as smtp:
        with pytest.raises(smtplib.SMTPAuthenticationError) as refused:
            smtp.login(USER, 'not the password')

    assert refused.value.smtp_code == 535

    mailpit.assert_nothing_delivered()


def test_relaying_through_an_authenticated_upstream(postfix_factory, mailpit_factory):
    """The relay authenticates to the server it relays to.

    The credentials come from a POSTMAP_ table, the way one configures a
    provider that only accepts authenticated mail.
    """
    upstream = mailpit_factory('upstream', users=[MailpitUser('relayuser', 'relaypass')])

    relay = postfix_factory(env={
        'POSTFIX_relayhost': 'upstream:1025',
        'POSTFIX_smtp_sasl_auth_enable': 'yes',
        'POSTFIX_smtp_sasl_password_maps': 'hash:/etc/postfix/sasl_passwd',
        'POSTFIX_smtp_sasl_security_options': 'noanonymous',
        'POSTMAP_sasl_passwd': 'upstream:1025 relayuser:relaypass',
    })

    send(relay, subject='relayed with credentials')

    assert upstream.wait_for_message('relayed with credentials')


def test_relaying_without_the_upstream_credentials_is_refused(postfix_factory,
                                                              mailpit_factory):
    upstream = mailpit_factory('upstream-strict',
                               users=[MailpitUser('relayuser', 'relaypass')])

    relay = postfix_factory(env={'POSTFIX_relayhost': 'upstream-strict:1025'})

    send(relay, subject='no credentials')

    wait_for_log(relay, 'Authentication required')
    upstream.assert_nothing_delivered()

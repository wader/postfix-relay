"""SMTP authentication, on both sides of the relay.

Clients authenticating to the relay (issues #69 and #79) and the relay
authenticating to the server it hands the mail over to, which is what
relaying through a provider such as gmail or sendgrid needs.
"""

import smtplib

import docker
import pytest

from testcontainers.community.mailpit import MailpitUser

from tests.helpers import (container_exec, container_log, restart, send,
                           smtp_connect, wait_for_log)

PASSWD_FILE = '/etc/postfix/sasl/sasl_passwds'
USER, PASSWORD = 'myuser', 'mypassword'


def mkpasswd(image, password):
    """Hash a password the way the README tells users to."""
    output = docker.from_env().containers.run(
        image, ["mkpasswd", "-m", "sha-512", password], remove=True)
    return output.decode().strip()


# The environment of the "Example Basic Client PAM Auth" README section.
AUTHENTICATED = {
    'SASL_Passwds': PASSWD_FILE,
    'POSTFIX_smtpd_sasl_auth_enable': 'yes',
    'POSTFIX_cyrus_sasl_config_path': '/etc/postfix/sasl',
    'POSTFIX_smtpd_sasl_security_options': 'noanonymous',
    'POSTFIX_smtpd_relay_restrictions': 'permit_sasl_authenticated,reject',
}


@pytest.fixture(scope="session")
def password_file(postfix_image):
    """The passwd file the README has users build, hashed once per run.

    mkpasswd salts every call, so hashing the password again for each test
    would give every relay a configuration of its own and none of them could
    be shared.
    """
    return {PASSWD_FILE: f"{USER}:{mkpasswd(postfix_image, PASSWORD)}\n"}


@pytest.fixture
def authenticated_relay(postfix_shared, password_file):
    """Relay set up like the "Example Basic Client PAM Auth" README section."""
    return postfix_shared(env=AUTHENTICATED, files=password_file)


@pytest.fixture
def restartable_authenticated_relay(postfix_factory, password_file):
    """The same relay, for the tests that stop and start it."""
    return postfix_factory(env=AUTHENTICATED, files=password_file)


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


def test_authentication_still_works_after_a_restart(restartable_authenticated_relay,
                                                    mailpit):
    """The second start must not leave the relay unable to authenticate anyone.

    Setting SASL up is not idempotent on the face of it: the statoverride is
    already registered and postfix is already in the sasl group, and neither
    failing stops the rest of the script.
    """
    restart(restartable_authenticated_relay)

    with smtp_connect(restartable_authenticated_relay) as smtp:
        smtp.ehlo()
        smtp.user, smtp.password = USER, PASSWORD
        code, _ = smtp.auth('PLAIN', smtp.auth_plain)
        assert code == 235

        smtp.sendmail('sender@example.com', ['receiver@example.com'],
                      'Subject: authenticated after restart\r\n\r\nbody\r\n')

    assert mailpit.wait_for_message('authenticated after restart')


def test_mounted_sasl_configuration_is_not_overwritten(postfix_image, postfix_factory):
    """A user can bring their own SASL and PAM configuration instead.

    Both files are only written when they do not exist, which is what makes
    mechanisms other than the built-in PAM setup possible.
    """
    smtpd_conf = 'pwcheck_method: saslauthd\nmech_list: PLAIN\n'
    pam = ('auth            required        pam_pwdfile.so pwdfile=%s\n'
           'account         required        pam_permit.so\n') % PASSWD_FILE

    relay = postfix_factory(
        env={
            'SASL_Passwds': PASSWD_FILE,
            'POSTFIX_smtpd_sasl_auth_enable': 'yes',
            'POSTFIX_cyrus_sasl_config_path': '/etc/postfix/sasl',
            'POSTFIX_smtpd_sasl_security_options': 'noanonymous',
        },
        files={
            PASSWD_FILE: f"{USER}:{mkpasswd(postfix_image, PASSWORD)}\n",
            '/etc/postfix/sasl/smtpd.conf': smtpd_conf,
            '/etc/pam.d/smtp': pam,
        },
    )

    assert container_exec(relay, ["cat", "/etc/postfix/sasl/smtpd.conf"]) == smtpd_conf
    assert container_exec(relay, ["cat", "/etc/pam.d/smtp"]) == pam

    with smtp_connect(relay) as smtp:
        smtp.ehlo()
        # The generated file would have offered CRAM-MD5, DIGEST-MD5 and LOGIN
        # as well, so the mounted mech_list is the one in use.
        assert smtp.esmtp_features['auth'].split() == ['PLAIN']

        smtp.user, smtp.password = USER, PASSWORD
        code, _ = smtp.auth('PLAIN', smtp.auth_plain)
        assert code == 235


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


# The upstream credentials, as the README's provider example configures them.
UPSTREAM_CREDENTIALS = {
    'POSTFIX_smtp_sasl_auth_enable': 'yes',
    'POSTFIX_smtp_sasl_password_maps': 'hash:/etc/postfix/sasl_passwd',
    'POSTMAP_sasl_passwd': 'upstream:1025 relayuser:relaypass',
}


def test_a_user_that_does_not_exist_is_refused(authenticated_relay):
    """The passwd file is the whole user list."""
    with smtp_connect(authenticated_relay) as smtp:
        with pytest.raises(smtplib.SMTPAuthenticationError) as refused:
            smtp.login('nosuchuser', PASSWORD)

    assert refused.value.smtp_code == 535


def test_a_refused_login_is_visible_in_the_container_log(authenticated_relay):
    """Otherwise a user whose credentials stopped working has nothing to
    look at: the log lines saslauthd writes itself go to the auth facility,
    which the generated rsyslog configuration keeps off stdout."""
    with smtp_connect(authenticated_relay) as smtp:
        with pytest.raises(smtplib.SMTPAuthenticationError):
            smtp.login(USER, 'not the password')

    assert 'SASL PLAIN authentication failed' in \
        wait_for_log(authenticated_relay, 'authentication failed')


def test_an_accepted_login_names_the_user_in_the_log(authenticated_relay, mailpit):
    """Which client sent which message is the one thing authentication adds
    to the log, and the only way to trace a message back to an account."""
    with smtp_connect(authenticated_relay) as smtp:
        smtp.ehlo()
        smtp.user, smtp.password = USER, PASSWORD
        smtp.auth('PLAIN', smtp.auth_plain)
        smtp.sendmail('sender@example.com', ['receiver@example.com'],
                      'Subject: named in the log\r\n\r\nbody\r\n')

    mailpit.wait_for_message('named in the log')

    assert f'sasl_username={USER}' in wait_for_log(authenticated_relay, 'sasl_username')


def test_saslauthd_answers_inside_the_postfix_chroot(authenticated_relay):
    """smtpd runs chrooted in the queue, so the socket it asks for a password
    check has to be inside the queue directory rather than in /var/run."""
    assert authenticated_relay.exec(
        ["test", "-S", "/var/spool/postfix/var/run/saslauthd/mux"]).exit_code == 0
    # And postfix is put in the group the socket is meant to belong to.
    assert 'sasl' in container_exec(authenticated_relay, ["id", "postfix"])


@pytest.mark.xfail(reason="known defect: the statoverride is recorded but never "
                          "applied, so the directory keeps the mode mkdir gave it",
                   strict=True)
def test_the_saslauthd_socket_is_restricted_to_the_sasl_group(authenticated_relay):
    """Anyone who can reach the socket can ask saslauthd to check a password.

    That is a password oracle with no rate limit, and the log lines it
    writes go to the auth facility, which the generated rsyslog
    configuration keeps off stdout. "run" asks for root:sasl 710 through
    dpkg-statoverride, and postfix is added to the group for it.
    """
    assert container_exec(
        authenticated_relay,
        "stat -c %U:%G:%a /var/spool/postfix/var/run/saslauthd").strip() == 'root:sasl:710'


def test_the_generated_pam_profile_reads_the_password_file(authenticated_relay):
    """SASL_Passwds names the file, and PAM is what actually opens it."""
    profile = container_exec(authenticated_relay, ["cat", "/etc/pam.d/smtp"])

    assert f'pam_pwdfile.so pwdfile={PASSWD_FILE}' in profile
    assert 'password        required        pam_deny.so' in profile


def test_the_generated_sasl_configuration_offers_the_documented_mechanisms(
        authenticated_relay):
    """The two that work with a PAM password check are offered along with
    the two that cannot: a client picking CRAM-MD5 first is why the tests
    name the mechanism instead of letting the library choose."""
    configuration = container_exec(
        authenticated_relay, ["cat", "/etc/postfix/sasl/smtpd.conf"])

    assert 'pwcheck_method: saslauthd' in configuration
    assert 'mech_list: CRAM-MD5 DIGEST-MD5 LOGIN PLAIN' in configuration


def test_the_upstream_password_never_reaches_the_log(postfix_shared):
    """docker logs is not a secret store, and the password of the provider
    account would otherwise be in every log collector this container feeds."""
    relay = postfix_shared(env=UPSTREAM_CREDENTIALS)

    assert 'relaypass' not in container_log(relay)


@pytest.mark.xfail(reason="known defect: the table is written with the default "
                          "umask, so every user in the container can read it",
                   strict=True)
def test_the_upstream_password_is_not_readable_by_every_user(postfix_shared):
    """It is a third party account's password in clear text.

    The DKIM keys and the SRS secret are both tightened to 600 on start-up;
    this file is the one that holds a password someone else issued, and
    opendkim -- which parses whatever a stranger sent -- can read it.
    """
    relay = postfix_shared(env=UPSTREAM_CREDENTIALS)

    assert container_exec(relay, "stat -c %a /etc/postfix/sasl_passwd").strip() == '600'

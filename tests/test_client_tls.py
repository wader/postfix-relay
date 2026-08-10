"""Encrypting the connections clients make to the relay.

The image offers no encryption by default: the README says docker networking
is the boundary, and the "Securing the relay" section is what to do when it
is not. These tests set the relay up the way that section documents.
"""

import re
import smtplib
import ssl

import pytest

from tests.helpers import image_run, mkpasswd, send, smtp_connect

CERTIFICATE = '/etc/postfix/tls/cert.pem'
KEY = '/etc/postfix/tls/key.pem'
PASSWD_FILE = '/etc/postfix/sasl/sasl_passwds'
USER, PASSWORD = 'myuser', 'mypassword'


@pytest.fixture(scope="session")
def certificate(postfix_image):
    """A self signed certificate and its key, generated once per run.

    With the image's own openssl, the way the README has an operator
    generate one, and handed to the relay as two files rather than through
    the environment because that is how a certificate is deployed.
    """
    output = image_run(postfix_image, [
        "sh", "-c",
        "cd /tmp && openssl req -x509 -newkey rsa:2048 -noenc -days 1 "
        "-subj /CN=smtp.example.com -keyout key.pem -out cert.pem 2> /dev/null && "
        "cat cert.pem && echo '===' && cat key.pem"])
    cert, key = output.split('===\n')
    return {CERTIFICATE: cert, KEY: key}


@pytest.fixture
def encrypting_relay(postfix_shared, certificate):
    """The configuration the README documents for encrypting client
    connections, with "may" so that plain clients still work."""
    return postfix_shared(
        env={
            'POSTFIX_smtpd_tls_cert_file': CERTIFICATE,
            'POSTFIX_smtpd_tls_key_file': KEY,
            'POSTFIX_smtpd_tls_security_level': 'may',
            'POSTFIX_smtpd_tls_auth_only': 'yes',
        },
        files=certificate)


def starttls(container, port=25):
    """Connect, ask for encryption and return the connected client.

    The certificate is self signed, which is deliberately not what any of
    this checks: a relay reached over a docker network is trusted because
    of where it is, and the point of the certificate is the encryption.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    smtp = smtp_connect(container, port)
    smtp.ehlo()
    code, _ = smtp.starttls(context=context)
    assert code == 220
    smtp.ehlo()
    return smtp


def test_clients_can_start_tls(encrypting_relay):
    with smtp_connect(encrypting_relay) as smtp:
        smtp.ehlo()

        assert smtp.has_extn('starttls')

    with starttls(encrypting_relay) as smtp:
        assert smtp.does_esmtp


def test_mail_can_be_relayed_over_an_encrypted_connection(encrypting_relay, mailpit):
    """Offering STARTTLS is not the same as being able to work over it."""
    with starttls(encrypting_relay) as smtp:
        smtp.sendmail('sender@example.com', ['receiver@example.com'],
                      'Subject: encrypted on the way in\r\n\r\nbody\r\n')

    assert mailpit.wait_for_message('encrypted on the way in')


def test_the_certificate_offered_is_the_one_that_was_mounted(encrypting_relay,
                                                             certificate):
    """A relay that fell back to a self generated one would encrypt just as
    well and be a different server as far as a verifying client is
    concerned."""
    with starttls(encrypting_relay) as smtp:
        offered = smtp.sock.getpeercert(binary_form=True)

    assert offered == ssl.PEM_cert_to_DER_cert(certificate[CERTIFICATE])


def relay_trace_header(mailpit, subject):
    """The Received header this relay added to a message."""
    received = mailpit.wait_for_message(subject)['headers']['received']
    return next(line for line in received if 'by hostname (Postfix)' in line)


def test_the_trace_header_says_the_hop_was_encrypted(encrypting_relay, mailpit):
    """"with ESMTPS" is what tells a receiver, or whoever reads the headers
    afterwards, that the message reached the relay over TLS. The cipher
    itself is only added when smtpd_tls_received_header is turned on."""
    with starttls(encrypting_relay) as smtp:
        smtp.sendmail('sender@example.com', ['receiver@example.com'],
                      'Subject: recorded encryption\r\n\r\nbody\r\n')

    assert re.search(r'with ESMTPS id', relay_trace_header(mailpit, 'recorded encryption'))


def test_plain_clients_are_still_accepted(encrypting_relay, mailpit):
    """"may" is what makes the change safe to deploy: the containers that
    were already sending mail keep working while clients move over."""
    send(encrypting_relay, subject='still plain')

    # And the trace header says so, "with ESMTP" rather than "with ESMTPS".
    assert re.search(r'with ESMTP id', relay_trace_header(mailpit, 'still plain'))


def test_encryption_can_be_made_compulsory(postfix_shared, certificate, mailpit):
    """"encrypt" is the setting the README names for a relay whose port is
    reachable from somewhere it should not be."""
    relay = postfix_shared(
        env={
            'POSTFIX_smtpd_tls_cert_file': CERTIFICATE,
            'POSTFIX_smtpd_tls_key_file': KEY,
            'POSTFIX_smtpd_tls_security_level': 'encrypt',
        },
        files=certificate)

    with smtp_connect(relay) as smtp:
        smtp.ehlo()
        code, message = smtp.docmd('MAIL', 'FROM:<sender@example.com>')

    assert code == 530
    assert b'Must issue a STARTTLS command first' in message

    with starttls(relay) as smtp:
        smtp.sendmail('sender@example.com', ['receiver@example.com'],
                      'Subject: had to encrypt\r\n\r\nbody\r\n')

    assert mailpit.wait_for_message('had to encrypt')


def test_credentials_are_only_asked_for_over_an_encrypted_connection(
        postfix_image, postfix_shared, certificate):
    """smtpd_tls_auth_only, the line the README comments with "never accept
    credentials over an unencrypted connection".

    A relay that advertised AUTH before STARTTLS would have clients send the
    password in the clear, and a client library picks what it is offered.
    """
    files = dict(certificate)
    files[PASSWD_FILE] = f"{USER}:{mkpasswd(postfix_image, PASSWORD)}\n"

    relay = postfix_shared(
        env={
            'SASL_Passwds': PASSWD_FILE,
            'POSTFIX_smtpd_sasl_auth_enable': 'yes',
            'POSTFIX_cyrus_sasl_config_path': '/etc/postfix/sasl',
            'POSTFIX_smtpd_sasl_security_options': 'noanonymous',
            'POSTFIX_smtpd_tls_cert_file': CERTIFICATE,
            'POSTFIX_smtpd_tls_key_file': KEY,
            'POSTFIX_smtpd_tls_security_level': 'may',
            'POSTFIX_smtpd_tls_auth_only': 'yes',
        },
        files=files)

    with smtp_connect(relay) as smtp:
        smtp.ehlo()

        assert not smtp.has_extn('auth')

        with pytest.raises(smtplib.SMTPNotSupportedError):
            smtp.login(USER, PASSWORD)

    with starttls(relay) as smtp:
        assert smtp.has_extn('auth')

        smtp.user, smtp.password = USER, PASSWORD

        assert smtp.auth('PLAIN', smtp.auth_plain)[0] == 235

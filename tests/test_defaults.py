"""What the image does when it is only told where to relay to.

DKIM, SASL and SRS are all off unless asked for, and the README warns that the
relay is open and unencrypted because docker networking is meant to be the
boundary. Those defaults are what every deployment that sets nothing gets, so
turning one of them on by accident would change all of them at once.

These tests share the default relay, so they cost no extra container.
"""

from tests.helpers import esmtp_features, postconf, process_running, send


def test_the_banner_announces_myhostname(postfix):
    code, banner, _ = esmtp_features(postfix)

    assert code == 220
    # "hostname" is the POSTFIX_myhostname default from the Dockerfile.
    assert banner.startswith('hostname ESMTP')


def test_the_usual_esmtp_capabilities_are_advertised(postfix):
    """What a client library checks for before using it."""
    _, _, features = esmtp_features(postfix)

    for capability in ['pipelining', 'size', '8bitmime', 'enhancedstatuscodes', 'dsn']:
        assert capability in features, features


def test_clients_are_offered_no_encryption(postfix):
    """POSTFIX_smtpd_tls_security_level=none, per the Dockerfile comment."""
    assert postconf(postfix, 'smtpd_tls_security_level') == 'none'

    _, _, features = esmtp_features(postfix)

    assert 'starttls' not in features


def test_mail_leaving_the_relay_still_uses_encryption_when_offered(postfix):
    """The other half of the same trade-off: only the client side is plain."""
    assert postconf(postfix, 'smtp_tls_security_level') == 'may'


def test_clients_are_offered_no_authentication(postfix):
    """SASL_Passwds is empty, so saslauthd is never started."""
    _, _, features = esmtp_features(postfix)

    assert 'auth' not in features
    assert not process_running(postfix, 'saslauthd')


def test_the_relay_is_open(postfix):
    """Documented, and the reason the README says not to expose the container."""
    assert postconf(postfix, 'mynetworks') == '0.0.0.0/0'
    assert postconf(postfix, 'mydestination') == 'localhost'


def test_nothing_is_signed(postfix, mailpit):
    assert not process_running(postfix, 'opendkim')
    assert postconf(postfix, 'smtpd_milters') == ''

    send(postfix, subject='unsigned by default')

    assert 'dkim-signature' not in mailpit.wait_for_message('unsigned by default')['headers']


def test_envelope_senders_are_not_rewritten(postfix, mailpit):
    assert not process_running(postfix, 'postsrsd')
    assert postconf(postfix, 'sender_canonical_maps') == ''

    send(postfix, sender='sender@example.com', subject='not rewritten')

    assert mailpit.wait_for_message('not rewritten')['ReturnPath'] == 'sender@example.com'

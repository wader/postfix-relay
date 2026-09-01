"""What the image does when it is only told where to relay to.

DKIM, SASL and SRS are all off unless asked for, and the README warns that the
relay is open and unencrypted because docker networking is meant to be the
boundary. Those defaults are what every deployment that sets nothing gets, so
turning one of them on by accident would change all of them at once.

These tests share the default relay, so they cost no extra container.
"""

import re

from tests.helpers import (esmtp_features, listening_ports, postconf,
                           process_running, send, wait_for_log)


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


def test_mail_leaving_the_relay_is_actually_encrypted(postfix_factory, mailpit):
    """And the setting above is not just stored: the handshake happens.

    smtp_tls_loglevel is raised only to make the connection observable, since
    postfix says nothing about it otherwise. The security level being exercised
    is the image default.
    """
    relay = postfix_factory(env={'POSTFIX_smtp_tls_loglevel': '1'})

    assert postconf(relay, 'smtp_tls_security_level') == 'may'

    send(relay, subject='over tls')
    mailpit.wait_for_message('over tls')

    log = wait_for_log(relay, 'TLS connection established to mailpit')

    assert re.search(r'TLS connection established to mailpit\[[\d.]+\]:1025: TLSv1', log), log


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


def test_only_the_smtp_port_is_open(postfix):
    """A port nobody asked for is a service nobody is watching.

    opendkim listens on 12301 and postsrsd on 10001 and 10002 once they are
    turned on, so an image that started them by default would say so here.
    """
    assert listening_ports(postfix) == {25}


def test_no_optional_daemon_is_running(postfix):
    """The three daemons the environment turns on, all off at once."""
    for daemon in ('opendkim', 'postsrsd', 'saslauthd'):
        assert not process_running(postfix, daemon), daemon


def test_the_relay_speaks_ipv4_only(postfix):
    """And the same everywhere, whoever built the image.

    Debian's postfix postinst writes inet_protocols into main.cf at install
    time from the IPv6 support of the machine running the build, so the same
    Dockerfile used to produce a dual stack image on a runner and an IPv4
    only one elsewhere. The Dockerfile pins it, which is what makes the
    README's "set POSTFIX_inet_protocols=all if your docker network has
    IPv6" a choice rather than a coin toss.
    """
    assert postconf(postfix, 'inet_protocols') == 'ipv4'
    assert 25 in listening_ports(postfix)


def test_utf8_addresses_are_supported(postfix):
    """SMTPUTF8, which is what an address with an accent in it needs."""
    _, _, features = esmtp_features(postfix)

    assert 'smtputf8' in features


def test_nothing_is_rewritten_on_the_way_through(postfix):
    """The rewriting SRS turns on is the only rewriting there is.

    All four maps are empty by default, so an address that goes in is the
    address that comes out, envelope and headers alike.
    """
    for parameter in ('sender_canonical_maps', 'recipient_canonical_maps',
                      'canonical_maps', 'smtp_generic_maps', 'masquerade_domains'):
        assert postconf(postfix, parameter) == '', parameter


def test_the_queue_is_where_the_image_declares_the_volume(postfix):
    """A queue somewhere else would be lost when the container is replaced,
    whatever the Dockerfile declares."""
    assert postconf(postfix, 'queue_directory') == '/var/spool/postfix'


def test_mail_for_any_domain_is_accepted(postfix, mailpit):
    """What "open relay" means, and the reason the README says not to publish
    the port. Two unrelated domains, neither of them configured anywhere."""
    send(postfix, recipients=('someone@first.example', 'someone@second.example'),
         subject='any domain at all')

    message = mailpit.wait_for_message('any domain at all')

    assert sorted(to['Address'] for to in message['To']) == \
        ['someone@first.example', 'someone@second.example']

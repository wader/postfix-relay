"""DKIM signing through OpenDKIM.

DKIM is the part of the image users have had the most trouble with, see
issues #14, #63, #78 and #92.
"""

from tests.helpers import container_exec, container_log, postconf, restart, send

KEY_PATH = '/etc/opendkim/keys/example.com/sel1.private'


def test_mail_is_signed_for_configured_domains_only(postfix_factory, mailpit):
    relay = postfix_factory(env={'OPENDKIM_DOMAINS': 'example.com=sel1'})

    # Enabling DKIM wires postfix to opendkim by itself.
    assert postconf(relay, 'smtpd_milters') == 'inet:localhost:12301'
    assert postconf(relay, 'milter_default_action') == 'accept'

    # The DNS record to publish is printed on start-up, it is the only place
    # a user gets it from when the key is generated in an anonymous volume.
    log = container_log(relay)
    assert 'sel1._domainkey.example.com' in log
    assert 'v=DKIM1' in log

    send(relay, sender='sender@example.com', subject='signed')
    send(relay, sender='sender@other.example', subject='not signed')

    signature = mailpit.wait_for_message('signed')['headers']['dkim-signature'][0]
    assert 'd=example.com' in signature
    assert 's=sel1' in signature

    # Only the domains in OPENDKIM_DOMAINS are in the signing table.
    assert 'dkim-signature' not in mailpit.wait_for_message('not signed')['headers']


def test_keys_are_reused_after_a_restart(postfix_factory, mailpit):
    """Restarting must not hand out a new key, the published DNS record
    would stop matching and every signed mail would fail validation."""
    relay = postfix_factory(env={'OPENDKIM_DOMAINS': 'example.com=sel1'})

    key = container_exec(relay, ["md5sum", KEY_PATH]).split()[0]

    restart(relay)

    assert container_exec(relay, ["md5sum", KEY_PATH]).split()[0] == key
    # The tables are rewritten from scratch instead of being appended to.
    assert container_exec(relay, ["wc", "-l", "/etc/opendkim/KeyTable"]).split()[0] == '1'
    # opendkim refuses to use a key other users could read.
    assert container_exec(relay, ["stat", "-c", "%U %a", KEY_PATH]).strip() == 'opendkim 600'

    send(relay, sender='sender@example.com', subject='signed after restart')

    assert 'dkim-signature' in mailpit.wait_for_message('signed after restart')['headers']


def test_milter_settings_are_left_alone_when_set_explicitly(postfix_factory):
    """Enabling DKIM must not overwrite milter settings the user set.

    Regression test for issue #134: the defaults are only there to save the
    user from having to configure the milter themselves.
    """
    relay = postfix_factory(env={
        'OPENDKIM_DOMAINS': 'example.com',
        'POSTFIX_milter_default_action': 'tempfail',
        'POSTFIX_smtpd_milters': 'inet:localhost:12301, inet:localhost:12302',
    })

    assert postconf(relay, 'milter_default_action') == 'tempfail'
    assert postconf(relay, 'smtpd_milters') == 'inet:localhost:12301, inet:localhost:12302'

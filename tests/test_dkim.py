"""DKIM signing through OpenDKIM.

DKIM is the part of the image users have had the most trouble with, see
issues #14, #63, #78 and #92.
"""

import dkim

from tests.helpers import (container_exec, container_log, dkim_dns_record, postconf,
                           restart, send)

KEY_PATH = '/etc/opendkim/keys/example.com/sel1.private'


def verifies(raw, record):
    """Whether a signed message validates against the record it points at.

    The public key is handed to the verifier directly instead of being looked
    up: there is no DNS in the test network, and what has to be checked is that
    the signature matches the record the container tells the user to publish.
    """
    return dkim.verify(raw, dnsfunc=lambda name, **kwargs: record.encode())


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


def test_the_private_key_is_only_readable_by_opendkim(postfix_factory):
    """OpenDKIM refuses a key other users can read, and logs instead of signing."""
    relay = postfix_factory(env={'OPENDKIM_DOMAINS': 'example.com=sel1'})

    assert container_exec(relay, f"stat -c %U:%G:%a {KEY_PATH}").strip() == 'opendkim:opendkim:600'


def test_the_directories_holding_the_keys_are_secured_too(postfix_factory):
    """Regression test for issue #92.

    opendkim walks the whole path down to a key and refuses one it could reach
    through a directory other users can write, naming that directory rather
    than the key. The key file's own mode being right is not enough, so a
    correct key was refused and the only answer the README had was to turn the
    check off with OPENDKIM_RequireSafeKeys=no.
    """
    relay = postfix_factory(env={'OPENDKIM_DOMAINS': 'example.com=sel1'})

    for path in ['/etc/opendkim/keys', '/etc/opendkim/keys/example.com']:
        assert container_exec(relay, ["stat", "-c", "%U:%G:%a", path]).strip() == \
            'opendkim:opendkim:700'


def test_a_key_volume_left_open_by_its_driver_is_corrected(docker_volume, postfix_factory,
                                                           mailpit):
    """The mode of /etc/opendkim/keys is whatever mounting left behind.

    The README blames this on volume handling in Docker for Windows and
    RancherOS. Nothing in the test suite can mount such a volume, but the fix
    is that the image stops trusting the mode it is given and sets it on every
    start, which a volume made writable by hand exercises just as well.
    """
    env = {'OPENDKIM_DOMAINS': 'example.com=sel1'}
    relay = postfix_factory(env=env, volumes={docker_volume: '/etc/opendkim/keys'})

    container_exec(relay, ["chmod", "-R", "a=rwx", "/etc/opendkim/keys"])

    restart(relay)

    assert container_exec(relay, ["stat", "-c", "%a", "/etc/opendkim/keys"]).strip() == '700'
    assert container_exec(relay, ["stat", "-c", "%a", KEY_PATH]).strip() == '600'

    # And the point of all of it: opendkim signs instead of logging that the
    # key data is not secure.
    send(relay, sender='sender@example.com', subject='signed despite the volume mode',
         body='signed body')

    raw = mailpit.raw(mailpit.wait_for_message('signed despite the volume mode')['ID'])

    assert verifies(raw, dkim_dns_record(relay, 'example.com', 'sel1'))


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


def test_the_signature_verifies_against_the_published_key(postfix_factory, mailpit):
    """A signature header being present is not the same as it being valid.

    Nothing else in the suite would notice a key, a selector or a
    canonicalization that produces a signature every receiver rejects.
    """
    relay = postfix_factory(env={'OPENDKIM_DOMAINS': 'example.com=sel1'})

    send(relay, sender='sender@example.com', subject='verify me', body='signed body')

    raw = mailpit.raw(mailpit.wait_for_message('verify me')['ID'])
    record = dkim_dns_record(relay, 'example.com', 'sel1')

    assert verifies(raw, record)
    # And it signs this message rather than always validating.
    assert not verifies(raw.replace(b'signed body', b'tampered!!!'), record)


def test_every_domain_gets_a_key_and_the_default_selector_is_mail(postfix_factory, mailpit):
    """OPENDKIM_DOMAINS is a whitespace-separated list, "=<selector>" optional."""
    relay = postfix_factory(env={
        'OPENDKIM_DOMAINS': 'first.example second.example=sel2',
        'OPENDKIM_Canonicalization': 'relaxed/relaxed',
        'OPENDKIM_RequireSafeKeys': 'no',
    })

    assert container_exec(relay, ["ls", "/etc/opendkim/keys/first.example"]).split() == \
        ['mail.private', 'mail.txt']
    assert container_exec(relay, ["ls", "/etc/opendkim/keys/second.example"]).split() == \
        ['sel2.private', 'sel2.txt']
    # opendkim refuses a key directory other users could write.
    assert container_exec(
        relay, ["stat", "-c", "%U:%G", "/etc/opendkim/keys/first.example"]
    ).strip() == 'opendkim:opendkim'

    # Every other OPENDKIM_<name> is an opendkim.conf setting, including the
    # RequireSafeKeys workaround the README documents...
    conf = container_exec(relay, ["cat", "/etc/opendkim.conf"])
    assert 'Canonicalization relaxed/relaxed' in conf
    assert 'RequireSafeKeys no' in conf
    # ...but the domain list itself is not one of them.
    assert 'DOMAINS' not in conf

    send(relay, sender='sender@first.example', subject='first domain')
    send(relay, sender='sender@second.example', subject='second domain')

    assert 's=mail' in mailpit.wait_for_message('first domain')['headers']['dkim-signature'][0]
    assert 's=sel2' in mailpit.wait_for_message('second domain')['headers']['dkim-signature'][0]


def test_keys_survive_recreating_the_container(docker_volume, postfix_factory, mailpit):
    """Mounting a volume on /etc/opendkim/keys keeps the published record valid.

    Restarting a container is not the case users are told about: the README
    warns the keys are destroyed with the container unless a volume is mounted,
    which is a different code path from a restart.
    """
    env = {'OPENDKIM_DOMAINS': 'example.com=sel1'}
    volumes = {docker_volume: '/etc/opendkim/keys'}

    first = postfix_factory(env=env, volumes=volumes)
    key = container_exec(first, ["md5sum", KEY_PATH]).split()[0]
    record = dkim_dns_record(first, 'example.com', 'sel1')
    first.get_wrapped_container().stop()

    second = postfix_factory(env=env, volumes=volumes)

    assert container_exec(second, ["md5sum", KEY_PATH]).split()[0] == key
    assert 'Generating one now' not in container_log(second)

    send(second, sender='sender@example.com', subject='signed by the kept key',
         body='signed body')

    raw = mailpit.raw(mailpit.wait_for_message('signed by the kept key')['ID'])

    assert verifies(raw, record)


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

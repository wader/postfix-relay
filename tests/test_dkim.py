"""DKIM signing through OpenDKIM.

DKIM is the part of the image users have had the most trouble with, see
issues #14, #63, #78 and #92.
"""

import re

import dkim
import pytest

from tests.helpers import (container_exec, container_log, dkim_dns_record, image_run,
                           listening_sockets, postconf, restart, send)

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


SIGNING = {'OPENDKIM_DOMAINS': 'example.com=sel1'}


@pytest.fixture
def signing(postfix_shared):
    """A relay signing for one domain, shared by the tests that only read it."""
    return postfix_shared(env=SIGNING)


@pytest.fixture(scope="session")
def generated_keypair(postfix_image):
    """A DKIM key pair made outside the relay, as a user migrating one has.

    Generated with the image's own opendkim-genkey so that it is the same
    kind of key "run" would have written, and returned as the two files a
    user would mount.
    """
    output = image_run(postfix_image, [
        "sh", "-c",
        "cd /tmp && opendkim-genkey --selector=sel1 --domain=example.com "
        "--append-domain && cat sel1.private && echo '===' && cat sel1.txt"])
    private, text = output.split('===\n')
    return private, text


def test_the_generated_key_is_long_enough_to_be_accepted(signing):
    """Receivers treat a short key as no signature at all.

    opendkim-genkey picks the size, so a base image update is what would
    change it, and a 1024 bit key still verifies here while being refused
    by the providers this relay hands mail to.
    """
    key = container_exec(signing, [
        "openssl", "rsa", "-in", KEY_PATH, "-noout", "-text"])

    assert 'Private-Key: (2048 bit' in key


def test_the_tables_name_the_domain_the_selector_and_the_key(signing):
    """What opendkim reads to decide whether and how to sign."""
    assert container_exec(signing, ["cat", "/etc/opendkim/KeyTable"]).strip() == \
        f'sel1._domainkey.example.com example.com:sel1:{KEY_PATH}'
    assert container_exec(signing, ["cat", "/etc/opendkim/SigningTable"]).strip() == \
        '*@example.com sel1._domainkey.example.com'


def test_the_record_printed_on_start_up_is_the_one_to_publish(signing):
    """The log is where a user gets it from when the key is generated in an
    anonymous volume, so it has to be the record itself and not a summary."""
    record = dkim_dns_record(signing, 'example.com', 'sel1')
    log = container_log(signing)

    assert record.startswith('v=DKIM1;')
    assert 'p=' in record
    # The log carries the file, quoted strings and all.
    for part in re.findall(r'"([^"]*)"', record):
        assert part in log or record in log


def test_the_published_key_is_the_public_half_of_the_generated_one(signing):
    """A record that does not match the key signs mail nobody can verify."""
    # openssl writes "writing RSA key" alongside the key itself, so the
    # base64 is taken from between the two markers rather than from
    # everything that is not one.
    public_key = container_exec(signing, [
        "openssl", "rsa", "-in", KEY_PATH, "-pubout"]).splitlines()
    begin = public_key.index('-----BEGIN PUBLIC KEY-----')
    end = public_key.index('-----END PUBLIC KEY-----')
    body = ''.join(public_key[begin + 1:end])

    assert f'p={body}' in dkim_dns_record(signing, 'example.com', 'sel1')


def test_opendkim_is_only_reachable_from_the_container(signing):
    """The milter socket is inet:12301@localhost.

    A milter listening on the docker network would let anything on it have
    mail signed with this domain's key.
    """
    milter = [address for address, port in listening_sockets(signing) if port == 12301]

    assert milter == ['127.0.0.1']


def test_a_subdomain_is_not_signed_by_its_parent_domain(signing, mailpit):
    """The signing table is "*@<domain>", which is the domain itself.

    Worth knowing rather than discovering from a receiver: mail from
    noreply@notifications.example.com is not covered by a key generated for
    example.com, each subdomain needs its own entry.
    """
    send(signing, sender='sender@sub.example.com', subject='from a subdomain')

    assert 'dkim-signature' not in mailpit.wait_for_message('from a subdomain')['headers']


def test_the_signature_covers_the_from_header(signing, mailpit):
    """The point of DKIM: the sender a receiver shows cannot be swapped out.

    A signature that only covered the body would verify on a message whose
    From was rewritten on the way.
    """
    send(signing, sender='sender@example.com', subject='signed from', body='body')

    message = mailpit.wait_for_message('signed from')
    raw = mailpit.raw(message['ID'])
    record = dkim_dns_record(signing, 'example.com', 'sel1')

    assert verifies(raw, record)
    assert 'h=' in message['headers']['dkim-signature'][0]
    assert not verifies(raw.replace(b'From: sender@example.com',
                                    b'From: someone@example.com'), record)


def test_a_domain_written_with_an_empty_selector_falls_back_to_mail(postfix_shared,
                                                                    mailpit):
    """"example.com=" is a domain with the separator and nothing after it,
    which the default has to cover rather than producing a key called ""."""
    relay = postfix_shared(env={'OPENDKIM_DOMAINS': 'example.com='})

    assert container_exec(relay, ["ls", "/etc/opendkim/keys/example.com"]).split() == \
        ['mail.private', 'mail.txt']

    send(relay, sender='sender@example.com', subject='empty selector')

    assert 's=mail' in mailpit.wait_for_message('empty selector')['headers'][
        'dkim-signature'][0]


def test_the_domain_list_may_be_written_over_several_lines(postfix_shared):
    """A compose file writes a long list as a block, which arrives with the
    newlines in it."""
    relay = postfix_shared(env={'OPENDKIM_DOMAINS': 'first.example\nsecond.example=sel2'})

    assert container_exec(relay, ["ls", "/etc/opendkim/keys/first.example"]).split() == \
        ['mail.private', 'mail.txt']
    assert container_exec(relay, ["ls", "/etc/opendkim/keys/second.example"]).split() == \
        ['sel2.private', 'sel2.txt']


def test_a_key_brought_from_somewhere_else_is_used_as_it_is(postfix_factory, mailpit,
                                                            generated_keypair):
    """Moving a relay must not mean republishing the DNS records.

    A key that is already there is not regenerated, and the permissions
    opendkim insists on are applied to it whoever wrote it.
    """
    private, text = generated_keypair

    relay = postfix_factory(env=SIGNING, files={
        KEY_PATH: private,
        '/etc/opendkim/keys/example.com/sel1.txt': text,
    })

    assert 'Generating one now' not in container_log(relay)
    assert container_exec(relay, ["cat", KEY_PATH]) == private
    assert container_exec(relay, f"stat -c %U:%a {KEY_PATH}").strip() == 'opendkim:600'

    send(relay, sender='sender@example.com', subject='signed by a mounted key',
         body='signed body')

    raw = mailpit.raw(mailpit.wait_for_message('signed by a mounted key')['ID'])

    assert verifies(raw, dkim_dns_record(relay, 'example.com', 'sel1'))


def test_a_signed_message_still_verifies_after_its_envelope_is_rewritten(
        postfix_shared, mailpit):
    """DKIM and SRS turned on together, which is what a forwarder runs.

    SRS rewrites the envelope sender and DKIM signs the headers, so the two
    have to stay out of each other's way: a rewrite that reached the From
    header would break every signature this relay produces.
    """
    relay = postfix_shared(env={'OPENDKIM_DOMAINS': 'example.com=sel1',
                                'POSTSRSD_SRS_DOMAIN': 'srs.example.com'})

    send(relay, sender='sender@example.com', subject='signed and rewritten',
         body='signed body')

    message = mailpit.wait_for_message('signed and rewritten')

    assert message['ReturnPath'].startswith('SRS0=')
    assert message['From']['Address'] == 'sender@example.com'
    assert verifies(mailpit.raw(message['ID']),
                    dkim_dns_record(relay, 'example.com', 'sel1'))

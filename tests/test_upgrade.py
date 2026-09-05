"""Starting on state an older image wrote.

Pulling a newer image replaces the whole filesystem, so what an upgrade
actually asks is whether the state left behind is still readable: the queue
and the DKIM keys in the two volumes, and the SRS secret in the file the
README says to mount. The Upgrading section promises the two volumes survive;
the SRS section promises the same of the secret, to whoever mounted it.

Everywhere else in the suite the container that writes that state and the one
that reads it are the same build, which is a restart rather than an upgrade
and cannot fail the way an upgrade fails. Here the state is written by the
last released image -- tests/upgrade-from.Dockerfile names it -- and read by
the one built from the tree, so a base suite bump that moves the queue
layout, the uid postfix runs as or the key directory has somewhere to show up
before it reaches a deployment.

Which release that is decides what can be covered: SRS landed after the one
named there, so the secret half of this skips itself until the anchor moves
past it, and says so rather than passing.
"""

import pytest

from tests.helpers import (container_exec, container_log, dkim_dns_record, image_run,
                           send, verifies, wait_for_log)

SRS_DOMAIN = 'srs.example.com'


def test_the_queue_the_older_image_left_is_delivered_by_this_one(upgrade_from_image,
                                                                 docker_volume,
                                                                 postfix_factory,
                                                                 mailpit_factory):
    """Mail accepted before the upgrade has been promised to its sender.

    The next hop being down is what keeps it in the queue long enough to be
    upgraded over, and is also the reason a user upgrades with mail in flight
    at all.
    """
    env = {'POSTFIX_relayhost': 'upgrade-target:1025'}
    volumes = {docker_volume: '/var/spool/postfix'}

    older = postfix_factory(image=upgrade_from_image, env=env, volumes=volumes)
    send(older, subject='queued before the upgrade')
    wait_for_log(older, 'status=deferred')
    older.get_wrapped_container().stop()

    upgraded = postfix_factory(env=env, volumes=volumes)

    assert 'receiver@example.com' in container_exec(upgraded, ["postqueue", "-p"])

    target = mailpit_factory('upgrade-target')
    container_exec(upgraded, ["postqueue", "-f"])

    assert target.wait_for_message('queued before the upgrade')


def test_the_key_the_older_image_generated_still_signs(upgrade_from_image, docker_volume,
                                                       postfix_factory, mailpit):
    """The published DNS record has to stay valid across an upgrade.

    It is the half of DKIM that is not in the image: the record was handed to
    a DNS zone, and a new image that generated a key of its own would leave
    every signature it makes failing verification until someone noticed and
    published the new record.
    """
    env = {'OPENDKIM_DOMAINS': 'example.com=sel1'}
    volumes = {docker_volume: '/etc/opendkim/keys'}

    older = postfix_factory(image=upgrade_from_image, env=env, volumes=volumes)
    published = dkim_dns_record(older, 'example.com', 'sel1')
    older.get_wrapped_container().stop()

    upgraded = postfix_factory(env=env, volumes=volumes)

    assert 'Generating one now' not in container_log(upgraded)

    send(upgraded, sender='sender@example.com', subject='signed after the upgrade',
         body='signed body')
    raw = mailpit.raw(mailpit.wait_for_message('signed after the upgrade')['ID'])

    assert verifies(raw, published)


@pytest.fixture
def older_image_that_rewrites(upgrade_from_image):
    """The released image, or a skip while it is older than SRS itself.

    SRS was added after the release tests/upgrade-from.Dockerfile names, so
    no deployment is upgrading from a release that ever wrote a secret and
    there is nothing yet for the test below to carry across. Asked of the
    image rather than written as a version to compare against: it starts
    running of its own accord the first time the anchor moves to a release
    that has the feature.

    The question is whether the image can rewrite, so it is put to the binary
    and not to the entrypoint's text. "run" names POSTSRSD_ on every
    architecture, armhf included, where the branch that names it is the one
    refusing to start because Debian builds no postsrsd there -- so grepping
    the script would answer "yes" for the one image that most certainly wrote
    no secret. "command -v postsrsd" is the same question "run" itself asks.
    """
    rewrites = image_run(upgrade_from_image, [
        "bash", "-c", "command -v postsrsd > /dev/null && echo yes || echo no"])

    if 'yes' not in rewrites:
        pytest.skip(f"{upgrade_from_image} has no postsrsd, so it left no secret to reuse")

    return upgrade_from_image


def test_addresses_the_older_image_rewrote_still_reverse(older_image_that_rewrites,
                                                         postfix_factory, mailpit):
    """A bounce can arrive days after the upgrade that replaced the relay.

    Return addresses are signed with /etc/postsrsd.secret, which is not in a
    volume: the README has users mount it, and this mounts what the older
    image generated. What has to hold is that the new postsrsd reverses an
    address the old one signed, rather than merely signing new ones of its own.
    """
    env = {'POSTSRSD_SRS_DOMAIN': SRS_DOMAIN}

    older = postfix_factory(image=older_image_that_rewrites, env=env)
    send(older, sender='sender@example.com', subject='forwarded before the upgrade')
    rewritten = mailpit.wait_for_message('forwarded before the upgrade')['ReturnPath']
    assert rewritten.startswith('SRS0=')

    secret = container_exec(older, ["cat", "/etc/postsrsd.secret"])
    older.get_wrapped_container().stop()

    upgraded = postfix_factory(env=env, files={'/etc/postsrsd.secret': secret})

    send(upgraded, sender='', recipients=(rewritten,), subject='the bounce after the upgrade')

    message = mailpit.wait_for_message('the bounce after the upgrade')

    # The envelope is what was decoded, which is what makes the bounce
    # deliverable; mailpit reports it as a Bcc because it matches no header.
    assert [to['Address'] for to in message['Bcc']] == ['sender@example.com']

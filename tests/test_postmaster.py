"""Where postfix's reports about itself are sent.

Postfix mails the postmaster when something is wrong with the relay itself.
Out of the box those notices are addressed to a mailbox the relay does not
accept mail for, so they are deferred until they time out: nobody is told
anything. POSTMASTER_ADDRESS points them at a mailbox someone reads.
"""

import pytest

from tests.helpers import postconf, send

# The four recipients POSTMASTER_ADDRESS points at.
NOTICE_CLASSES = ('error', 'bounce', '2bounce', 'delay')

POSTMASTER = 'ops@example.com'
EXPLICIT = 'errors-only@example.com'

# A relay that reports its bounces. Nothing routes "nowhere.example" and it
# does not resolve, so a message to it is accepted and then bounced, which is
# what a notice is sent about.
REPORTING = {
    'POSTMASTER_ADDRESS': POSTMASTER,
    'POSTFIX_notify_classes': 'bounce',
    'POSTFIX_transport_maps': 'hash:/etc/postfix/transport',
    'POSTMAP_transport': 'nowhere.example smtp:[nothing.invalid]:25',
}
UNDELIVERABLE = 'receiver@nowhere.example'


@pytest.fixture
def relay_postmaster(postfix_shared):
    return postfix_shared(env={'POSTMASTER_ADDRESS': POSTMASTER})


@pytest.fixture
def relay_explicit(postfix_shared):
    return postfix_shared(env={'POSTMASTER_ADDRESS': POSTMASTER,
                               'POSTFIX_error_notice_recipient': EXPLICIT})


@pytest.fixture
def relay_empty(postfix_shared):
    return postfix_shared(env={'POSTMASTER_ADDRESS': ''})


def test_notice_recipients_untouched_without_postmaster_address(postfix):
    # The whole block sits inside "if POSTMASTER_ADDRESS is set", so a
    # container that does not set it must come up exactly as it did before.
    for notice_class in NOTICE_CLASSES:
        assert postconf(postfix, f"{notice_class}_notice_recipient") == 'postmaster'


def test_postmaster_address_sets_all_notice_recipients(relay_postmaster):
    for notice_class in NOTICE_CLASSES:
        assert postconf(relay_postmaster, f"{notice_class}_notice_recipient") == POSTMASTER


def test_explicit_notice_recipient_wins(relay_explicit):
    # POSTFIX_error_notice_recipient is applied by the generic POSTFIX_ loop,
    # which runs after; the POSTMASTER_ADDRESS block must leave it alone.
    assert postconf(relay_explicit, 'error_notice_recipient') == EXPLICIT


def test_explicit_notice_recipient_does_not_affect_other_classes(relay_explicit):
    for notice_class in ('bounce', '2bounce', 'delay'):
        assert postconf(relay_explicit, f"{notice_class}_notice_recipient") == POSTMASTER


def test_notify_classes_left_at_postfix_default(relay_postmaster):
    # Naming recipients must not change which problems get reported, so the
    # volume of notification mail is the same as before.
    assert postconf(relay_postmaster, 'notify_classes') == 'resource, software'


def test_2bounce_notice_recipient_accepts_leading_digit(relay_postmaster):
    # "2bounce_notice_recipient" is the one parameter name here that starts
    # with a digit -- check postconf -e took it rather than failing the start.
    stdout, stderr = relay_postmaster.get_logs()
    assert 'fatal' not in (stdout + stderr).decode(errors='replace')
    assert postconf(relay_postmaster, '2bounce_notice_recipient') == POSTMASTER


def test_myhostname_and_myorigin_untouched(relay_postmaster):
    # Routing the notices is deliberately independent of the myhostname
    # default; setting POSTMASTER_ADDRESS must not quietly change the HELO
    # name or the envelope sender of locally generated mail.
    assert postconf(relay_postmaster, 'myhostname') == 'hostname'
    assert postconf(relay_postmaster, 'myorigin') == '$myhostname'


def test_empty_postmaster_address_is_inert(relay_empty):
    for notice_class in NOTICE_CLASSES:
        assert postconf(relay_empty, f"{notice_class}_notice_recipient") == 'postmaster'


def test_a_notice_actually_reaches_the_address(postfix_shared, mailpit):
    """The setting is not the point, the mail arriving is.

    Everything above reads the configuration back. This one widens
    notify_classes to bounces, makes a message bounce, and waits for the
    copy postfix sends the postmaster to come out of the relay -- which is
    what setting the address was for.
    """
    relay = postfix_shared(env=REPORTING)

    send(relay, recipients=(UNDELIVERABLE,), subject='undeliverable')

    notice = mailpit.wait_for_message('Postmaster Copy: Undelivered Mail')

    assert [to['Address'] for to in notice['To']] == [POSTMASTER]
    assert 'Host or domain name not found' in notice['Text']


def test_the_sender_still_gets_its_own_bounce(postfix_shared, mailpit):
    """The postmaster copy is a copy: telling the operator must not stop the
    person who sent the message from being told."""
    relay = postfix_shared(env=REPORTING)

    send(relay, sender='sender@example.com', recipients=(UNDELIVERABLE,),
         subject='also bounced to the sender')

    bounce = mailpit.wait_for_message('Undelivered Mail Returned to Sender')

    assert [to['Address'] for to in bounce['To']] == ['sender@example.com']

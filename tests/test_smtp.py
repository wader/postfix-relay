"""The SMTP conversation itself, on the relay everyone gets by default.

A relay is a message in and the same message out. What is checked here is
that side of it: the commands a client may use, what the relay answers, and
that the message it hands over is the one it was given. Everything runs
against the shared default relay, so none of it costs a container.
"""

import re
import smtplib
import threading

import pytest

from tests.helpers import (esmtp_features, postconf, send, send_raw, smtp_connect,
                           wait_for_log)


def raw_body(mailpit, message):
    """The message as it arrived, split at the blank line after the headers."""
    return mailpit.raw(message['ID']).split(b'\r\n\r\n', 1)[1]


def logged_message_id(container, recipient):
    """The Message-ID postfix recorded for the message sent to a recipient.

    Read from the relay's own log rather than from what arrived: mailpit
    fills in a Message-ID of its own when a message has none, so it cannot
    answer whether the relay added one. Postfix logs "message-id=<>" for a
    message that had none.
    """
    log = wait_for_log(container, f'to=<{recipient}>')
    queue_id = re.search(rf'(\w+): to=<{re.escape(recipient)}>', log).group(1)
    return re.search(rf'{queue_id}: message-id=(\S+)', log).group(1)


def test_helo_is_accepted_as_well_as_ehlo(postfix, mailpit):
    """A client that speaks plain SMTP has to be able to relay too."""
    smtp = smtplib.SMTP(postfix.get_container_host_ip(),
                        postfix.get_exposed_port(25), timeout=30)
    try:
        code, _ = smtp.helo('client.example')
        assert code == 250

        smtp.sendmail('sender@example.com', ['receiver@example.com'],
                      'Subject: plain smtp\r\n\r\nbody\r\n')
    finally:
        smtp.quit()

    assert mailpit.wait_for_message('plain smtp')


def test_noop_is_answered(smtp):
    smtp.ehlo()

    assert smtp.noop()[0] == 250


def test_an_unknown_command_is_refused_without_dropping_the_connection(smtp):
    smtp.ehlo()

    assert smtp.docmd('FROBNICATE')[0] == 500
    # And the session is still usable, a client that tried something
    # optional must not have to reconnect.
    assert smtp.noop()[0] == 250


def test_rset_abandons_the_message_being_written(smtp, mailpit):
    smtp.ehlo()
    smtp.docmd('MAIL', 'FROM:<sender@example.com>')
    smtp.docmd('RCPT', 'TO:<receiver@example.com>')

    assert smtp.rset()[0] == 250

    # The envelope is gone, so DATA has nothing to attach a message to.
    assert smtp.docmd('DATA')[0] == 503
    mailpit.assert_nothing_delivered()


def test_data_before_a_recipient_is_refused(smtp):
    smtp.ehlo()
    smtp.docmd('MAIL', 'FROM:<sender@example.com>')

    code, message = smtp.docmd('DATA')

    assert code == 554
    assert b'no valid recipients' in message


def test_a_recipient_before_a_sender_is_refused(smtp):
    smtp.ehlo()

    code, message = smtp.docmd('RCPT', 'TO:<receiver@example.com>')

    assert code == 503
    assert b'Error: need MAIL command' in message


def test_several_messages_may_share_one_connection(smtp, mailpit):
    """Which is what a client with a queue of its own does."""
    for number in (1, 2, 3):
        smtp.sendmail('sender@example.com', ['receiver@example.com'],
                      f'Subject: message {number}\r\n\r\nbody\r\n')

    for number in (1, 2, 3):
        assert mailpit.wait_for_message(f'message {number}')


def test_one_message_reaches_all_of_its_recipients(postfix, mailpit):
    """Postfix groups recipients per next hop, so twenty of them are one
    delivery, and losing one of the group would be invisible in the log."""
    recipients = [f'receiver_{number}@example.com' for number in range(20)]

    send(postfix, recipients=recipients, subject='twenty recipients')

    message = mailpit.wait_for_message('twenty recipients')

    assert sorted(to['Address'] for to in message['To']) == sorted(recipients)


def test_a_line_that_is_only_a_dot_survives(postfix, mailpit):
    """A dot on its own ends the message, so it has to be escaped and
    unescaped again. Getting that wrong truncates every message that has a
    line starting with one, quoted text and diffs being the usual sources."""
    body = 'before\r\n.\r\n. after a dot\r\n..two dots\r\nend\r\n'

    send_raw(postfix, f'Subject: dots\r\n\r\n{body}')

    message = mailpit.wait_for_message('dots')

    assert raw_body(mailpit, message) == body.encode()


def test_an_empty_body_is_relayed(postfix, mailpit):
    send_raw(postfix, 'Subject: nothing to say\r\n\r\n')

    assert mailpit.wait_for_message('nothing to say')


def test_the_relay_does_not_invent_a_message_id(postfix, mailpit):
    """A relay is not the message's author.

    Postfix can fill in the headers a message is missing, and does not by
    default: a Message-ID minted here would not match the one the sending
    application logged.
    """
    assert postconf(postfix, 'always_add_missing_headers') == 'no'

    send_raw(postfix, 'Subject: no message id\r\nFrom: sender@example.com\r\n\r\nbody\r\n',
             recipients=('no-message-id@example.com',))
    mailpit.wait_for_message('no message id')

    assert logged_message_id(postfix, 'no-message-id@example.com') == '<>'


def test_an_existing_message_id_is_kept(postfix, mailpit):
    send_raw(postfix,
             'Subject: with a message id\r\n'
             'Message-ID: <kept@sender.example>\r\n\r\nbody\r\n',
             recipients=('kept-message-id@example.com',))
    mailpit.wait_for_message('with a message id')

    assert logged_message_id(postfix, 'kept-message-id@example.com') == \
        '<kept@sender.example>'


def test_headers_the_relay_knows_nothing_about_are_passed_on(postfix, mailpit):
    """Everything an application puts in front of the body has to arrive:
    List-Unsubscribe, Auto-Submitted, whatever the next hop filters on."""
    send_raw(postfix,
             'Subject: custom headers\r\n'
             'X-Application: invoice-service\r\n'
             'List-Unsubscribe: <https://example.com/unsubscribe>\r\n'
             '\r\nbody\r\n')

    headers = mailpit.wait_for_message('custom headers')['headers']

    assert headers['x-application'] == ['invoice-service']
    assert headers['list-unsubscribe'] == ['<https://example.com/unsubscribe>']


def test_a_repeated_header_keeps_all_of_its_occurrences(postfix, mailpit):
    """Received and Comments legitimately appear several times, and a relay
    that folded them into one would rewrite the message's history."""
    send_raw(postfix,
             'Subject: repeated\r\n'
             'Comments: first\r\n'
             'Comments: second\r\n'
             '\r\nbody\r\n')

    headers = mailpit.wait_for_message('repeated')['headers']

    assert headers['comments'] == ['first', 'second']


def test_the_relay_adds_exactly_one_received_header(postfix, mailpit):
    """One hop, one trace header: two would make the path look longer than
    it is, and none would lose the relay from it entirely."""
    send_raw(postfix,
             'Subject: one hop\r\n'
             'Received: from somewhere.example by before.example; '
             'Mon, 1 Jan 2024 00:00:00 +0000\r\n'
             '\r\nbody\r\n')

    received = mailpit.wait_for_message('one hop')['headers']['received']

    assert len([line for line in received if 'by hostname (Postfix)' in line]) == 1
    # The one that was already there is kept, below the new one.
    assert 'by before.example' in received[-1]


def test_an_eight_bit_body_without_mime_headers_survives(postfix, mailpit):
    """Not every sender declares its encoding, and a relay does not get to
    decide that a message is not worth passing on unchanged."""
    body = 'Ceci n\'est pas du MIME : déjà vu\r\n'.encode()

    send_raw(postfix, b'Subject: eight bit\r\n\r\n' + body)

    message = mailpit.wait_for_message('eight bit')

    assert raw_body(mailpit, message) == body


def test_the_body_is_relayed_byte_for_byte(postfix, mailpit):
    """Including the bytes that are not text at all: a relay that re-encoded
    a body would break every signature over it."""
    body = bytes(range(0x20, 0x7f)) + b'\r\n' + bytes(range(0x80, 0x100)) + b'\r\n'

    send_raw(postfix, b'Subject: every byte\r\nMIME-Version: 1.0\r\n'
                      b'Content-Transfer-Encoding: binary\r\n\r\n' + body)

    message = mailpit.wait_for_message('every byte')

    assert raw_body(mailpit, message) == body


def test_a_message_with_no_headers_at_all_is_relayed(postfix, mailpit):
    """Postfix supplies the headers a message needs to be legal mail, but
    the body still has to arrive."""
    send_raw(postfix, 'a body and nothing else\r\n')

    message = mailpit.wait_for_message()

    assert 'a body and nothing else' in message['Text']


def test_the_advertised_size_is_the_limit_that_is_enforced(postfix):
    """A client reads SIZE to decide whether to try at all, so it has to be
    the number the relay actually refuses above."""
    _, _, features = esmtp_features(postfix)

    assert features['size'] == postconf(postfix, 'message_size_limit')


def test_an_unqualified_address_is_qualified_with_the_relay_name(postfix, mailpit):
    """append_at_myorigin, which is why the README asks for a real myhostname.

    An address with no domain is completed with myorigin, which derives from
    myhostname. With the shipped placeholder that is the literal "hostname",
    a domain that is in nobody's mydestination and resolves nowhere, so the
    mail is relayed on to a name that does not exist rather than delivered.
    """
    send_raw(postfix, 'Subject: unqualified\r\n\r\nbody\r\n',
             recipients=('nosuchuser',))

    message = mailpit.wait_for_message('unqualified')

    assert [to['Address'] for to in message['Bcc']] == ['nosuchuser@hostname']


def test_a_quoted_local_part_is_relayed(postfix, mailpit):
    """Legal, rare, and the kind of address that gets mangled on the way.

    RFC 5321 4.1.2 defines a local part as a Dot-string *or* a Quoted-string,
    so the quotes are part of the address and a relay that drops them changes
    who the message is for.

    Read from what the relay handed to the next hop rather than from what the
    next hop did with it. Those are different questions, and only the first is
    this image's: mailpit answers "553 5.1.3 The address is not a valid RFC
    5321 address" to this recipient from v1.28.3 on, so asserting on the
    stored message made the test a test of the peer, and pinned the suite to
    a mailpit older than that. The peer is still asked for, so that this is a
    real delivery attempt to a server that is there rather than a deferral.
    """
    send(postfix, recipients=('"odd user"@example.com',), subject='quoted local part')

    handed_over = wait_for_log(postfix, 'to=<"odd user"@example.com>')

    assert 'to=<"odd user"@example.com>' in handed_over


def test_an_address_with_a_plus_is_relayed_untouched(postfix, mailpit):
    """The relay must not strip the part after the delimiter: it is what the
    receiving mailbox files the message by."""
    send(postfix, recipients=('receiver+invoices@example.com',), subject='plus addressing')

    message = mailpit.wait_for_message('plus addressing')

    assert message['To'][0]['Address'] == 'receiver+invoices@example.com'


def test_clients_may_relay_at_the_same_time(postfix, mailpit):
    """Ten containers sending at once is the normal load of this image."""
    errors = []

    def relay(number):
        try:
            send(postfix, subject=f'concurrent {number}')
        except Exception as error:            # noqa: BLE001 - reported below
            errors.append(error)

    senders = [threading.Thread(target=relay, args=(number,)) for number in range(10)]
    for sender in senders:
        sender.start()
    for sender in senders:
        sender.join(timeout=60)

    assert errors == []
    for number in range(10):
        assert mailpit.wait_for_message(f'concurrent {number}')


def test_a_refused_message_leaves_the_session_usable(smtp, mailpit):
    """A client that hit a limit goes on to the next message on the same
    connection, which it may only do if the relay kept the session."""
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        smtp.sendmail('sender@example.com', ['nosuchuser@localhost'],
                      'Subject: refused\r\n\r\nbody\r\n')

    smtp.sendmail('sender@example.com', ['receiver@example.com'],
                  'Subject: after a refusal\r\n\r\nbody\r\n')

    assert mailpit.wait_for_message('after a refusal')


def test_a_message_the_relay_accepted_is_reported_as_sent(postfix, mailpit):
    """status=sent in the log is what an operator greps for, and it has to
    mean the next hop took the message."""
    send(postfix, subject='logged as sent')
    message = mailpit.wait_for_message('logged as sent')

    log = wait_for_log(postfix, 'status=sent')

    assert message['ID']
    assert 'to=<receiver@example.com>' in log


def test_the_queue_is_empty_once_the_mail_is_gone(postfix, mailpit):
    """Anything left behind is mail an operator has to chase."""
    from tests.helpers import container_exec, poll_until

    send(postfix, subject='nothing left behind')
    mailpit.wait_for_message('nothing left behind')

    poll_until(lambda: 'Mail queue is empty' in container_exec(postfix, ["postqueue", "-p"]),
               description="the queue to drain")


def test_the_banner_names_postfix(postfix):
    """smtpd_banner is left at its default, so the relay says what it is.

    Hiding it is a documented postfix setting rather than something this
    image does, and a receiver that logs the banner sees the same string
    every version of the image has sent.
    """
    _, banner, _ = esmtp_features(postfix)

    assert banner.strip().endswith('ESMTP Postfix (Debian)')


def test_a_client_that_disappears_does_not_deliver_half_a_message(postfix, mailpit):
    """A connection lost inside DATA must not leave a truncated message in
    the queue: the message is only accepted once the final dot arrived."""
    smtp = smtp_connect(postfix)
    smtp.ehlo()
    smtp.docmd('MAIL', 'FROM:<sender@example.com>')
    smtp.docmd('RCPT', 'TO:<receiver@example.com>')
    smtp.docmd('DATA')
    smtp.send(b'Subject: half a message\r\n\r\nthe beginning')
    smtp.close()

    mailpit.assert_nothing_delivered()

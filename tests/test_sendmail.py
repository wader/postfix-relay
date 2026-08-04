import os

from email.message import EmailMessage
from email.utils import make_msgid
from email.headerregistry import Address


def test_sendmail(mailpit, smtp):
    # Send email to postfix
    msg = EmailMessage()
    msg['Subject'] = 'Hello world'
    msg['From'] = Address('Sender', 'sender', 'example.com')
    msg['To'] = (Address('Receiver 1', 'receiver_1', 'example.com'),
                 Address('Receiver 2', 'receiver_2', 'example.com'))

    text = """
    Salut!

    Cette recette [1] sera sûrement un très bon repas.

    [1] https://example.com

    --Pepé
    """

    msg.set_content(text)

    cid = make_msgid()
    html = """
    <html>
    <head></head>
    <body>
        <p>Salut!</p>
        <p>Cette
            <a href="https://example.com">
                recette
            </a> sera sûrement un très bon repas.
        </p>
        <img src="cid:{cid}">
    </body>
    </html>
    """.format(cid=cid[1:-1])

    msg.add_alternative(html, subtype='html')

    root_path = os.path.dirname(__file__)
    with open(f"{root_path}/img/postfix-logo.png", 'rb') as img:
        msg.get_payload()[1].add_related(img.read(), 'image', 'png', cid=cid)

    smtp.send_message(msg)

    # On mailpit check if the email exists, then check its content.
    message = mailpit.wait_for_message('Hello world')

    assert message['From']['Address'] == 'sender@example.com'
    assert message['To'][0]['Address'] == 'receiver_1@example.com'
    assert message['To'][1]['Address'] == 'receiver_2@example.com'

    expected_text = text.replace('\n', '\r\n') + '\r\n'
    expected_html = html.replace('\n', '\r\n') + '\r\n'

    assert message['Text'] == expected_text
    assert message['HTML'] == expected_html

    assert len(message['Inline']) == 1
    assert message['Inline'][0]['ContentType'] == 'image/png'


def test_relayed_message_keeps_its_envelope(mailpit, smtp):
    """The envelope, not the headers, decides where a relay sends mail.

    A bcc recipient only exists in the envelope, so a message reaching one is
    the proof that postfix relayed what it was given rather than what the
    headers say.
    """
    msg = EmailMessage()
    msg['Subject'] = 'Envelope'
    msg['From'] = 'sender@example.com'
    msg['To'] = 'visible@example.com'
    msg.set_content('body')

    smtp.sendmail('bounces@example.com',
                  ['visible@example.com', 'hidden@example.com'],
                  msg.as_string())

    message = mailpit.wait_for_message('Envelope')

    assert message['ReturnPath'] == 'bounces@example.com'
    assert [to['Address'] for to in message['To']] == ['visible@example.com']
    # Mailpit reports the envelope recipients that are in no header as bcc.
    assert [bcc['Address'] for bcc in message['Bcc']] == ['hidden@example.com']


def test_relay_adds_its_own_received_header(mailpit, smtp):
    """Postfix identifies itself with myhostname in the trace headers."""
    smtp.sendmail('sender@example.com', ['receiver@example.com'],
                  'Subject: Received\r\n\r\nbody\r\n')

    message = mailpit.wait_for_message('Received')

    # "hostname" is the myhostname default from the Dockerfile.
    assert any('by hostname (Postfix)' in received
               for received in message['headers']['received'])

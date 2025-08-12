import os
import time
import requests

from email.message import EmailMessage
from email.utils import make_msgid
from email.headerregistry import Address

def test_sendmail(mailpit, smtp):
    # Send email to postfix
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
    
    time.sleep(1)

    # On mailpit check if the email exists
    api_url = f"{mailpit.get_base_api_url()}/api/v1"

    response = requests.get(f"{api_url}/messages")
    json = response.json()

    assert json['total'] == 1

    # Then check its content.
    message_summary = json['messages'][0]

    assert message_summary['From']['Address'] == 'sender@example.com'
    assert message_summary['To'][0]['Address'] == 'receiver_1@example.com'
    assert message_summary['To'][1]['Address'] == 'receiver_2@example.com'

    response = requests.get(f"{api_url}/message/{message_summary['ID']}")
    json = response.json()

    expected_text = text.replace('\n', '\r\n') + '\r\n'
    expected_html = html.replace('\n', '\r\n') + '\r\n'

    assert json['Text'] == expected_text
    assert json['HTML'] == expected_html
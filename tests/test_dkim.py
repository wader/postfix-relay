import dkim
import re
import smtplib
import time
import requests

from email.message import EmailMessage

from tests.fixtures.dkim import DOMAIN, SELECTOR

def published_record(container):
    # opendkim-genkey writes the record as a BIND fragment, split over quoted
    # strings the way a zone file wants it. Put it back together as the DNS
    # answer a verifier would get.
    _, output = container.exec(f"cat /etc/opendkim/keys/{DOMAIN}/{SELECTOR}.txt")

    return "".join(re.findall(r'"([^"]*)"', output.decode())).encode()

def relayed_message(mailpit, timeout=30):
    api_url = f"{mailpit.get_base_api_url()}/api/v1"
    deadline = time.monotonic() + timeout

    while True:
        messages = requests.get(f"{api_url}/messages").json()['messages']
        if messages:
            raw = requests.get(f"{api_url}/message/{messages[0]['ID']}/raw").content
            # Line endings have to be what was signed for the body hash to
            # match, whatever the store and the transport did with them.
            return re.sub(rb'\r?\n', b'\r\n', raw)
        if time.monotonic() > deadline:
            raise AssertionError("no message was relayed")
        time.sleep(0.5)

def test_relayed_mail_is_signed(dkim_mailpit, signing_postfix):
    msg = EmailMessage()
    msg['Subject'] = 'Signed'
    msg['From'] = f"sender@{DOMAIN}"
    msg['To'] = 'receiver@example.org'
    msg.set_content('Hello')

    with smtplib.SMTP(host=signing_postfix.get_container_host_ip(),
                      port=signing_postfix.get_exposed_port(port=25)) as smtp:
        smtp.send_message(msg)

    raw = relayed_message(dkim_mailpit)
    signature = re.search(rb'^DKIM-Signature:(.*(?:\r\n[ \t].*)*)', raw, re.MULTILINE)

    assert signature, "message was relayed without a DKIM-Signature header"
    assert f"d={DOMAIN}".encode() in signature.group(1)
    assert f"s={SELECTOR}".encode() in signature.group(1)

    # And it verifies against the record the container told the operator to
    # publish, which is the part that decides whether receivers accept it.
    record = published_record(signing_postfix)

    assert dkim.verify(raw, dnsfunc=lambda name, timeout=5: record)

def test_private_key_is_only_readable_by_opendkim(signing_postfix):
    # OpenDKIM refuses keys other users can read, and says so in the log
    # instead of signing.
    _, output = signing_postfix.exec(
        f"stat -c %U:%G:%a /etc/opendkim/keys/{DOMAIN}/{SELECTOR}.private")

    assert output.decode().strip() == 'opendkim:opendkim:600'

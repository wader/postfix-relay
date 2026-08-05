import time

import pytest
import requests

from testcontainers.community.mailpit import MailpitContainer

from tests.conftest import print_log_on_failure
from tests.helpers import poll_until

IMAGE = "axllent/mailpit:v1.27"


class Mailpit:
    """The mailpit REST API, as the tests need it.

    Mailpit stands in for the remote SMTP server the relay delivers to, so
    what reaches it is what the outside world would have received.
    """

    def __init__(self, container):
        self.api_url = f"{container.get_base_api_url()}/api/v1"

    def clear(self):
        requests.delete(f"{self.api_url}/messages").raise_for_status()

    def summaries(self):
        response = requests.get(f"{self.api_url}/messages")
        response.raise_for_status()
        return response.json()['messages']

    def message(self, message_id):
        response = requests.get(f"{self.api_url}/message/{message_id}")
        response.raise_for_status()
        return response.json()

    def raw(self, message_id):
        """The message exactly as it arrived, bytes included.

        Anything that has to be checked byte for byte, a DKIM signature for
        instance, has to be read from here rather than from the parsed JSON.
        """
        response = requests.get(f"{self.api_url}/message/{message_id}/raw")
        response.raise_for_status()
        return response.content

    def part(self, message_id, part_id):
        """One MIME part of a message, as bytes."""
        response = requests.get(f"{self.api_url}/message/{message_id}/part/{part_id}")
        response.raise_for_status()
        return response.content

    def headers(self, message_id):
        """Headers of a message, keyed by lower case name."""
        response = requests.get(f"{self.api_url}/message/{message_id}/headers")
        response.raise_for_status()
        return {name.lower(): values for name, values in response.json().items()}

    def wait_for_message(self, subject=None, timeout=30):
        """Wait for a relayed message and return it, with its headers."""
        def delivered():
            return next((summary for summary in self.summaries()
                         if subject is None or summary['Subject'] == subject), None)

        summary = poll_until(delivered, timeout=timeout,
                             description=f"message {subject!r} to be relayed")

        message = self.message(summary['ID'])
        message['headers'] = self.headers(summary['ID'])
        return message

    def assert_nothing_delivered(self, seconds=5):
        """Check that nothing shows up, for mail that must not be relayed."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            assert self.summaries() == [], "a message was relayed but none should have been"
            time.sleep(0.5)


@pytest.fixture(scope="session")
def mailpit_container(shared_network):
    container = MailpitContainer(IMAGE) \
        .with_network(shared_network) \
        .with_network_aliases('mailpit')

    container.start()
    yield container
    container.stop()


@pytest.fixture
def mailpit(mailpit_container):
    """The relay target shared by the tests, emptied before each of them."""
    client = Mailpit(mailpit_container)
    client.clear()

    return client


@pytest.fixture
def mailpit_factory(shared_network, request):
    """Start extra relay targets, for tests that need their own.

        upstream = mailpit_factory('upstream', users=[MailpitUser('user', 'pass')])
    """
    started = []

    def start(alias, users=None):
        container = MailpitContainer(IMAGE, users=users) \
            .with_network(shared_network) \
            .with_network_aliases(alias)

        container.start()
        started.append((alias, container))
        return Mailpit(container)

    yield start

    for alias, container in reversed(started):
        print_log_on_failure(request, f"mailpit ({alias})", container)
        container.stop()

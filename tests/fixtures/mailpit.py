import functools
import time

import docker
import pytest
import requests

import testcontainers.community.mailpit as mailpit_module

from testcontainers.community.mailpit import MailpitContainer

# Ask the container whether it is up more often than once a second.
#
# MailpitContainer.start() waits for a line in the log, and looks for it at the
# library's default interval of one second. Mailpit is ready 0.19s after docker
# starts it, measured, so the first look is too early and the second one is most
# of a second late: 1.33s to start one against 0.29s when it is asked more
# often, and the suite starts about nine of them.
#
# The wait itself is not changed -- same log line, same timeout, and it still
# returns the moment the line is there rather than after a fixed delay. Only how
# often it looks.
#
# Done by handing the library's own function a different default rather than by
# reimplementing start() in a subclass: if a release stops going through
# wait_for_logs, this quietly stops applying and the suite is merely as slow as
# it is today, where a start() of our own would silently skip whatever that
# release had added to theirs.
mailpit_module.wait_for_logs = functools.partial(
    mailpit_module.wait_for_logs, interval=0.05)

from tests.conftest import print_log_on_failure
from tests.helpers import once_across_workers, poll_until

IMAGE = "axllent/mailpit:v1.27.11"


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


def _pull_if_absent():
    """Fetch the image, unless this machine already has it.

    IMAGE names an exact version, so an image that is here is the image a
    pull would bring: asking for it again is a registry round trip that buys
    nothing and spends one of the hundred anonymous pulls Docker Hub allows
    per six hours. Past that it answers 429, every mailpit fixture fails to
    set up, and a hundred tests error out looking like a regression.

    A runner starts without it and pulls exactly as before.
    """
    client = docker.from_env()

    try:
        client.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        client.images.pull(IMAGE)


@pytest.fixture(scope="session")
def mailpit_image(tmp_path_factory):
    """The mailpit image, fetched once for the whole run.

    Starting a container pulls the image it needs, so without this every
    worker would pull this one at the same moment as the others.
    """
    once_across_workers(tmp_path_factory, "mailpit-image", _pull_if_absent)

    return IMAGE


@pytest.fixture(scope="session")
def mailpit_container(shared_network, mailpit_image):
    container = MailpitContainer(mailpit_image) \
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
def mailpit_factory(shared_network, mailpit_image, request):
    """Start extra relay targets, for tests that need their own.

        upstream = mailpit_factory('upstream', users=[MailpitUser('user', 'pass')])
    """
    started = []

    def start(alias, users=None):
        container = MailpitContainer(mailpit_image, users=users) \
            .with_network(shared_network) \
            .with_network_aliases(alias)

        container.start()
        started.append((alias, container))
        return Mailpit(container)

    yield start

    for alias, container in reversed(started):
        print_log_on_failure(request, f"mailpit ({alias})", container)
        container.stop()

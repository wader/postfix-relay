import pytest

from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs
from testcontainers.mailpit import MailpitContainer

DOMAIN = 'example.com'
SELECTOR = 'relay'

@pytest.fixture(scope="session")
def dkim_mailpit(shared_network):
    container = MailpitContainer("axllent/mailpit:v1.27") \
        .with_network(shared_network) \
        .with_network_aliases('dkim-mailpit')

    container.start()
    yield container
    container.stop()

@pytest.fixture(scope="session")
def signing_postfix(shared_network, postfix_image, dkim_mailpit):
    container = DockerContainer(image=postfix_image) \
        .with_network(shared_network) \
        .with_network_aliases('signing-postfix') \
        .with_exposed_ports(25) \
        .with_env('POSTFIX_relayhost', 'dkim-mailpit:1025') \
        .with_env('OPENDKIM_DOMAINS', f"{DOMAIN}={SELECTOR}")

    container.start()

    wait_for_logs(container, "Starting the Postfix mail system", timeout=30)

    yield container
    container.stop()

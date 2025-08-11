import pytest
from testcontainers.mailpit import MailpitContainer

@pytest.fixture(scope="session")
def mailpit(shared_network):
    container = MailpitContainer("axllent/mailpit:v1.27") \
        .with_network(shared_network) \
        .with_network_aliases('mailpit')

    container.start()
    yield container
    container.stop()
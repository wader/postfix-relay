import os
import pytest

from testcontainers.core.container import DockerContainer
from testcontainers.core.image import DockerImage
from testcontainers.core.waiting_utils import wait_for_logs

@pytest.fixture(scope="session")
def postfix(shared_network):
    root_path = os.path.dirname(__file__) + '/../../'
    image = DockerImage(path=root_path, tag="postfix-relay:test")

    image.build()

    container = DockerContainer(image=str(image)) \
        .with_network(shared_network) \
        .with_network_aliases('postfix') \
        .with_exposed_ports(25) \
        .with_env('POSTFIX_relayhost', 'mailpit:1025')

    container.start()

    wait_for_logs(container, "Starting Postfix Mail Transport Agent: postfix", timeout=10)

    yield container
    container.stop()
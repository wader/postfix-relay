import pytest
import docker

@pytest.fixture(scope="session")
def shared_network():
    network = docker.from_env().networks.create("test-shared-net", driver="bridge")
    yield network
    network.remove()
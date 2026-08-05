import pytest

from testcontainers.core.network import Network


@pytest.fixture(scope="session")
def shared_network():
    """Network the relay and its mail servers talk over.

    A testcontainers network rather than a fixed name: it gets a name of its
    own for every run and is labelled, so a run that is interrupted before
    its teardown leaves nothing behind for the next one to collide with.
    """
    with Network() as network:
        yield network

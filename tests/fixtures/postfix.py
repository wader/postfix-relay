import itertools
import os

import docker
import pytest

from testcontainers.core.container import DockerContainer
from testcontainers.core.image import DockerImage

from tests.conftest import print_log_on_failure
from tests.helpers import poll_until, wait_for_smtp

ROOT_PATH = os.path.dirname(__file__) + '/../../'

# Containers sharing a network need distinct aliases.
_alias_numbers = itertools.count(1)


@pytest.fixture(scope="session")
def postfix_image():
    """The image under test, built once for the whole session."""
    image = DockerImage(path=ROOT_PATH, tag="postfix-relay:test")

    image.build()

    return str(image)


def _start(image, network, env=None, files=None, volumes=None, ports=(25,), alias=None,
           wait_ready=True):
    container = DockerContainer(image=image) \
        .with_network(network) \
        .with_network_aliases(alias or f"postfix-{next(_alias_numbers)}") \
        .with_env('POSTFIX_relayhost', 'mailpit:1025')

    for port in ports:
        container.with_exposed_ports(port)
    for name, path in (volumes or {}).items():
        container.with_volume_mapping(name, path, 'rw')
    # Set after the default above so that a test can override it.
    for name, value in (env or {}).items():
        container.with_env(name, value)
    # Copied before the container starts, so "run" sees them like a mounted
    # file would look, without needing anything writable on the host.
    for path, content in (files or {}).items():
        container.with_copy_into_container(content.encode(), path)

    container.start()

    if wait_ready:
        wait_for_smtp(container, port=ports[0])

    return container


@pytest.fixture(scope="session")
def postfix(postfix_image, shared_network):
    """Relay with the image default configuration, shared by all tests."""
    container = _start(postfix_image, shared_network, alias='postfix')

    yield container

    container.stop()


@pytest.fixture
def postfix_factory(postfix_image, shared_network, request):
    """Start relays configured for a single test.

    Every call starts a container, so prefer the "postfix" fixture when the
    default configuration is enough.

        relay = postfix_factory(env={'OPENDKIM_DOMAINS': 'example.com'})

    "files" writes files into the container before it starts, for the
    configuration that is documented as mounted instead of set through
    environment variables. "volumes" maps a docker volume name to a path, for
    the state the image is documented to keep across containers.
    "wait_ready" can be turned off for containers that are not expected to
    come up at all.
    """
    started = []

    def start(env=None, files=None, volumes=None, ports=(25,), alias=None, wait_ready=True):
        container = _start(postfix_image, shared_network, env=env, files=files,
                           volumes=volumes, ports=ports, alias=alias, wait_ready=wait_ready)
        started.append(container)
        return container

    yield start

    for number, container in reversed(list(enumerate(started, start=1))):
        print_log_on_failure(request, f"postfix ({number})", container)
        container.stop()


@pytest.fixture
def docker_volume():
    """An empty docker volume, for the state the image keeps across containers.

        relay = postfix_factory(volumes={docker_volume: '/etc/opendkim/keys'})
    """
    volume = docker.from_env().volumes.create()

    yield volume.name

    # The containers using it are removed by the fixtures that started them,
    # which pytest may tear down after this one, and docker refuses to remove
    # a volume still attached to a container.
    poll_until(lambda: _removed(volume), timeout=30,
               description=f"volume {volume.name} to be removable")


def _removed(volume):
    try:
        volume.remove()
    except docker.errors.APIError:
        return False
    return True

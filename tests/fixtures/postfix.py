import itertools
import os

import docker
import pytest

from testcontainers.core.container import DockerContainer
from testcontainers.core.image import DockerImage

from tests.conftest import print_log_on_failure
from tests.helpers import once_across_workers, poll_until, wait_for_smtp

ROOT_PATH = os.path.dirname(__file__) + '/../../'

IMAGE_TAG = "postfix-relay:test"

# An image built outside the suite, for the runs that cannot build their own:
# DockerImage builds through the docker daemon, which cannot cross-build, so a
# foreign architecture has to be built with buildx and loaded first. Unset,
# which is every run on the machine it is meant for, the suite builds the image
# as it always has.
#
# Only accepted for an architecture this machine cannot build, or for the image
# that was published (see below): between them that is the whole of the case for
# having it. Building from the Dockerfile is what makes the suite test the tree
# it is run in rather than whatever was left in the image store, and that is not
# worth giving up anywhere it can still be done.
PREBUILT_IMAGE = os.environ.get('POSTFIX_RELAY_IMAGE')

# What that image has to be, as docker reports it: "arm" for the arm/v7 image,
# "arm64", "amd64". Without this a job that meant to test another architecture
# and got the runner's own would pass every test it ran and say nothing, which
# is the one way an emulated run can be worse than no run at all.
EXPECTED_ARCHITECTURE = os.environ.get('POSTFIX_RELAY_ARCH')

# The other image the suite cannot build for itself: the one buildx pushed to
# the registry, which is what a user pulls and the only thing no check here
# looks at otherwise. It is the architecture of the runner that pulls it, so
# the refusal below would turn it down for looking like a stale local build.
# This is how a caller says it is not one, and it is the only thing that lifts
# that refusal: a run that names a leftover image by accident still gets it.
PUBLISHED_IMAGE = os.environ.get('POSTFIX_RELAY_IMAGE_PUBLISHED')

# Containers sharing a network need distinct aliases.
_alias_numbers = itertools.count(1)


@pytest.fixture(scope="session")
def postfix_image(tmp_path_factory):
    """The image under test, built once for the whole run.

    Every test reaches the image through here, so pointing this at a prebuilt
    one is enough to run the suite against an image the suite did not build --
    a foreign architecture, or the image that was published.
    """
    if PREBUILT_IMAGE:
        client = docker.from_env()
        image = client.images.get(PREBUILT_IMAGE)
        architecture = image.attrs['Architecture']

        if not PUBLISHED_IMAGE and architecture == client.version()['Arch']:
            raise AssertionError(
                f"POSTFIX_RELAY_IMAGE names a {architecture} image, which this "
                "machine can build: the suite builds from the Dockerfile so "
                "that what it tests is what is in the tree. It takes a "
                "prebuilt image only where the docker builder cannot make one, "
                "or where POSTFIX_RELAY_IMAGE_PUBLISHED says the image named is "
                "the one that was published rather than a build of the tree.")

        if EXPECTED_ARCHITECTURE and architecture != EXPECTED_ARCHITECTURE:
            raise AssertionError(
                f"{PREBUILT_IMAGE} is {architecture}, not {EXPECTED_ARCHITECTURE}: "
                "the tests would have passed against the wrong architecture")

        return PREBUILT_IMAGE

    once_across_workers(
        tmp_path_factory, "postfix-image",
        lambda: DockerImage(path=ROOT_PATH, tag=IMAGE_TAG).build())

    return IMAGE_TAG


def _start(image, network, env=None, files=None, volumes=None, ports=(25,), alias=None,
           command=None, wait_ready=True, kwargs=None, register=None):
    container = DockerContainer(image=image) \
        .with_network(network) \
        .with_network_aliases(alias or f"postfix-{next(_alias_numbers)}") \
        .with_env('POSTFIX_relayhost', 'mailpit:1025')

    if command is not None:
        container.with_command(command)
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
    # Straight to docker, for what a test needs to say about the container
    # itself rather than about its configuration, such as capabilities.
    if kwargs:
        container.with_kwargs(**kwargs)

    container.start()
    # Handed over before the wait rather than after it: a relay that never
    # comes up makes the wait raise, and a caller that only learns about the
    # container when _start returns would neither stop it nor have it to show
    # the log of -- which is the one thing that says why it did not come up.
    if register is not None:
        register(container)

    if wait_ready:
        wait_for_smtp(container, port=ports[0])

    return container


@pytest.fixture(scope="session")
def postfix(postfix_image, shared_network):
    """Relay with the image default configuration, shared by all tests."""
    started = []
    try:
        container = _start(postfix_image, shared_network, alias='postfix',
                           register=started.append)

        yield container
    finally:
        for container in started:
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
    "command" replaces the image's own, for the few tests that have to stand
    the image up differently from the way it ships. "wait_ready" can be turned
    off for containers that are not expected to come up at all, and "kwargs" is
    passed to docker as is.
    """
    started = []

    def start(env=None, files=None, volumes=None, ports=(25,), alias=None, command=None,
              wait_ready=True, kwargs=None):
        return _start(postfix_image, shared_network, env=env, files=files,
                      volumes=volumes, ports=ports, alias=alias, command=command,
                      wait_ready=wait_ready, kwargs=kwargs, register=started.append)

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


@pytest.fixture(scope="session")
def _relay_pool(postfix_image, shared_network):
    """Relays kept for the whole session, one per configuration asked for.

    Starting a container is most of what the suite costs, so the tests that
    only read a relay back -- what it advertises, what it wrote, what it does
    with a message -- share one instead of starting their own.
    """
    containers = {}

    yield containers

    for container in reversed(list(containers.values())):
        container.stop()


@pytest.fixture
def postfix_shared(_relay_pool, postfix_image, shared_network, request):
    """Relay shared by every test asking for the same configuration.

        relay = postfix_shared(env={'OPENDKIM_DOMAINS': 'example.com'})

    It takes the same "env", "files" and "ports" as "postfix_factory".
    The first test to ask for a configuration starts it, the ones after reuse
    it. That only holds for tests that leave the container as they found it:
    anything that kills a daemon, edits a file or restarts the container has
    to start its own with "postfix_factory".
    """
    used = []

    def start(env=None, files=None, ports=(25,)):
        key = (tuple(sorted((env or {}).items())),
               tuple(sorted((files or {}).items())), tuple(ports))
        if key not in _relay_pool:
            _start(postfix_image, shared_network, env=env, files=files, ports=ports,
                   register=lambda container: _relay_pool.__setitem__(key, container))
        container = _relay_pool[key]
        used.append(container)
        return container

    yield start

    for number, container in enumerate(used, start=1):
        print_log_on_failure(request, f"shared postfix ({number})", container)

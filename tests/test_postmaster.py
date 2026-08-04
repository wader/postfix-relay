import os
import pytest

from testcontainers.core.container import DockerContainer
from testcontainers.core.image import DockerImage
from testcontainers.core.waiting_utils import wait_for_logs

# The four recipients POSTMASTER_ADDRESS points at.
NOTICE_CLASSES = ('error', 'bounce', '2bounce', 'delay')

POSTMASTER = 'ops@example.com'
EXPLICIT = 'errors-only@example.com'


@pytest.fixture(scope="module")
def postfix_image():
    root_path = os.path.dirname(__file__) + '/../'
    image = DockerImage(path=root_path, tag="postfix-relay:test")
    image.build()
    return str(image)


def start_relay(image, env):
    container = DockerContainer(image=image)
    for name, value in env.items():
        container.with_env(name, value)

    container.start()
    wait_for_logs(container, "Starting", timeout=60)

    return container


@pytest.fixture(scope="module")
def relay_default(postfix_image):
    container = start_relay(postfix_image, {})
    yield container
    container.stop()


@pytest.fixture(scope="module")
def relay_postmaster(postfix_image):
    container = start_relay(postfix_image, {'POSTMASTER_ADDRESS': POSTMASTER})
    yield container
    container.stop()


@pytest.fixture(scope="module")
def relay_explicit(postfix_image):
    container = start_relay(postfix_image, {
        'POSTMASTER_ADDRESS': POSTMASTER,
        'POSTFIX_error_notice_recipient': EXPLICIT,
    })
    yield container
    container.stop()


@pytest.fixture(scope="module")
def relay_empty(postfix_image):
    container = start_relay(postfix_image, {'POSTMASTER_ADDRESS': ''})
    yield container
    container.stop()


def postconf(container, name):
    exit_code, output = container.exec(['postconf', '-h', name])
    assert exit_code == 0, output.decode()
    return output.decode().strip()


def test_notice_recipients_untouched_without_postmaster_address(relay_default):
    # The whole block sits inside "if POSTMASTER_ADDRESS is set", so a
    # container that does not set it must come up exactly as it did before.
    for c in NOTICE_CLASSES:
        assert postconf(relay_default, f"{c}_notice_recipient") == 'postmaster'


def test_postmaster_address_sets_all_notice_recipients(relay_postmaster):
    for c in NOTICE_CLASSES:
        assert postconf(relay_postmaster, f"{c}_notice_recipient") == POSTMASTER


def test_explicit_notice_recipient_wins(relay_explicit):
    # POSTFIX_error_notice_recipient is applied by the generic POSTFIX_ loop,
    # which runs first; the POSTMASTER_ADDRESS block must leave it alone.
    assert postconf(relay_explicit, 'error_notice_recipient') == EXPLICIT


def test_explicit_notice_recipient_does_not_affect_other_classes(relay_explicit):
    for c in ('bounce', '2bounce', 'delay'):
        assert postconf(relay_explicit, f"{c}_notice_recipient") == POSTMASTER


def test_notify_classes_left_at_postfix_default(relay_postmaster):
    # Naming recipients must not change which problems get reported, so the
    # volume of notification mail is the same as before.
    assert postconf(relay_postmaster, 'notify_classes') == 'resource, software'


def test_2bounce_notice_recipient_accepts_leading_digit(relay_postmaster):
    # "2bounce_notice_recipient" is the one parameter name here that starts
    # with a digit -- check postconf -e took it rather than failing the start.
    stdout, stderr = relay_postmaster.get_logs()
    assert 'fatal' not in (stdout + stderr).decode(errors='replace')
    assert postconf(relay_postmaster, '2bounce_notice_recipient') == POSTMASTER


def test_myhostname_and_myorigin_untouched(relay_postmaster):
    # Routing the notices is deliberately independent of the myhostname
    # default; setting POSTMASTER_ADDRESS must not quietly change the HELO
    # name or the envelope sender of locally generated mail.
    assert postconf(relay_postmaster, 'myhostname') == 'hostname'
    assert postconf(relay_postmaster, 'myorigin') == '$myhostname'


def test_empty_postmaster_address_is_inert(relay_empty):
    for c in NOTICE_CLASSES:
        assert postconf(relay_empty, f"{c}_notice_recipient") == 'postmaster'

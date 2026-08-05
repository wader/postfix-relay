import pytest

from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs
from testcontainers.mailpit import MailpitContainer, MailpitUser

# Mailpit only accepts these credentials, so a delivered message proves postfix
# authenticated with what the mounted secret file holds.
RELAY_USER = MailpitUser("relay", "s3cret")
RELAY_HOST = "[relay-mailpit]:1025"

@pytest.fixture(scope="session")
def relay_mailpit(shared_network):
    container = MailpitContainer("axllent/mailpit:v1.27", users=[RELAY_USER]) \
        .with_network(shared_network) \
        .with_network_aliases('relay-mailpit')

    container.start()
    yield container
    container.stop()

@pytest.fixture(scope="session")
def sasl_passwd_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("secrets") / "sasl_passwd"
    path.write_text(f"{RELAY_HOST} {RELAY_USER.username}:{RELAY_USER.password}\n")

    return path

@pytest.fixture(scope="session")
def relaying_postfix(shared_network, postfix_image, relay_mailpit, sasl_passwd_file):
    container = DockerContainer(image=postfix_image) \
        .with_network(shared_network) \
        .with_network_aliases('relaying-postfix') \
        .with_exposed_ports(25) \
        .with_volume_mapping(str(sasl_passwd_file), '/run/secrets/sasl_passwd', 'ro') \
        .with_env('POSTFIX_relayhost', RELAY_HOST) \
        .with_env('POSTFIX_smtp_sasl_auth_enable', 'yes') \
        .with_env('POSTFIX_smtp_sasl_password_maps', 'hash:/etc/postfix/sasl_passwd') \
        .with_env('POSTFIX_smtp_sasl_security_options', 'noanonymous') \
        .with_env('POSTFIX_smtp_tls_security_level', 'encrypt') \
        .with_env('POSTMAP_sasl_passwd_FILE', '/run/secrets/sasl_passwd')

    container.start()

    wait_for_logs(container, "Starting", timeout=10)

    yield container
    container.stop()

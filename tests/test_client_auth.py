import docker
import pytest
import smtplib

from email.message import EmailMessage

from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

USER = 'myuser'
PASSWORD = 'mypassword'

@pytest.fixture(scope="session")
def passwd_file(postfix_image, tmp_path_factory):
    # The way the README tells users to create it.
    hashed = docker.from_env().containers.run(
        postfix_image, ["mkpasswd", "-m", "sha-512", PASSWORD], remove=True)

    path = tmp_path_factory.mktemp("sasl") / "passwd_file"
    path.write_text(f"{USER}:{hashed.decode().strip()}\n")
    path.chmod(0o644)

    return path

@pytest.fixture(scope="session")
def authenticating_postfix(postfix_image, passwd_file):
    # The configuration the README documents for client authentication, with
    # relaying restricted to authenticated clients.
    container = DockerContainer(image=postfix_image) \
        .with_exposed_ports(25) \
        .with_volume_mapping(str(passwd_file), '/etc/postfix/sasl/sasl_passwds', 'ro') \
        .with_env('SASL_Passwds', '/etc/postfix/sasl/sasl_passwds') \
        .with_env('POSTFIX_smtpd_sasl_auth_enable', 'yes') \
        .with_env('POSTFIX_cyrus_sasl_config_path', '/etc/postfix/sasl') \
        .with_env('POSTFIX_smtpd_sasl_security_options', 'noanonymous') \
        .with_env('POSTFIX_smtpd_relay_restrictions', 'permit_sasl_authenticated,reject')

    container.start()

    wait_for_logs(container, "Starting the Postfix mail system", timeout=30)

    yield container
    container.stop()

def connect(container):
    return smtplib.SMTP(host=container.get_container_host_ip(),
                        port=container.get_exposed_port(port=25))

def message():
    msg = EmailMessage()
    msg['Subject'] = 'Authenticated'
    msg['From'] = 'sender@example.com'
    msg['To'] = 'receiver@example.com'
    msg.set_content('Hello')

    return msg

def test_client_can_authenticate(authenticating_postfix):
    with connect(authenticating_postfix) as smtp:
        code, _ = smtp.login(USER, PASSWORD)

        assert code == 235

def test_wrong_password_is_rejected(authenticating_postfix):
    with connect(authenticating_postfix) as smtp:
        with pytest.raises(smtplib.SMTPAuthenticationError):
            smtp.login(USER, 'not the password')

def test_authenticated_client_may_relay(authenticating_postfix):
    with connect(authenticating_postfix) as smtp:
        smtp.login(USER, PASSWORD)

        assert smtp.send_message(message()) == {}

def test_unauthenticated_client_may_not_relay(authenticating_postfix):
    with connect(authenticating_postfix) as smtp:
        with pytest.raises(smtplib.SMTPRecipientsRefused):
            smtp.send_message(message())

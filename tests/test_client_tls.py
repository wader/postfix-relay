import docker
import pytest
import smtplib
import ssl

from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

@pytest.fixture(scope="session")
def certificate(postfix_image, tmp_path_factory):
    path = tmp_path_factory.mktemp("tls")

    # The image carries openssl, so the certificate an operator would generate
    # is generated the same way here.
    docker.from_env().containers.run(
        postfix_image,
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-noenc",
         "-days", "1", "-subj", "/CN=smtp.example.com",
         "-keyout", "/tls/key.pem", "-out", "/tls/cert.pem"],
        volumes={str(path): {'bind': '/tls', 'mode': 'rw'}},
        remove=True)

    return path

@pytest.fixture(scope="session")
def encrypting_postfix(postfix_image, certificate):
    # The configuration the README documents for encrypting client connections.
    container = DockerContainer(image=postfix_image) \
        .with_exposed_ports(25) \
        .with_volume_mapping(str(certificate), '/etc/postfix/tls', 'ro') \
        .with_env('POSTFIX_smtpd_tls_cert_file', '/etc/postfix/tls/cert.pem') \
        .with_env('POSTFIX_smtpd_tls_key_file', '/etc/postfix/tls/key.pem') \
        .with_env('POSTFIX_smtpd_tls_security_level', 'may') \
        .with_env('POSTFIX_smtpd_tls_auth_only', 'yes')

    container.start()

    wait_for_logs(container, "Starting the Postfix mail system", timeout=30)

    yield container
    container.stop()

def test_clients_can_start_tls(encrypting_postfix):
    context = ssl.create_default_context()
    # The certificate is self signed, which is what this checks nothing about.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with smtplib.SMTP(host=encrypting_postfix.get_container_host_ip(),
                      port=encrypting_postfix.get_exposed_port(port=25)) as smtp:
        smtp.ehlo()

        assert smtp.has_extn('starttls')

        code, _ = smtp.starttls(context=context)

        assert code == 220

        smtp.ehlo()

        assert smtp.does_esmtp

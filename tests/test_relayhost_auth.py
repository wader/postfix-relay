import docker
import smtplib
import time
import requests

from email.message import EmailMessage

from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from tests.fixtures.relayhost import RELAY_USER

def wait_for_healthcheck(container, expected, timeout=10):
    # A killed daemon takes a moment to actually go away, and docker retries
    # the check anyway, so wait for the expected verdict rather than catching
    # the container mid-signal.
    deadline = time.monotonic() + timeout

    while True:
        exit_code, output = container.exec("/root/healthcheck")
        if exit_code == expected or time.monotonic() > deadline:
            return exit_code, output
        time.sleep(0.5)

def wait_for_message(mailpit, timeout=30):
    api_url = f"{mailpit.get_base_api_url()}/api/v1"
    deadline = time.monotonic() + timeout

    while True:
        json = requests.get(f"{api_url}/messages").json()
        if json['total'] > 0 or time.monotonic() > deadline:
            return json
        time.sleep(0.5)

def test_relay_through_authenticated_server(relay_mailpit, relaying_postfix):
    msg = EmailMessage()
    msg['Subject'] = 'Relayed with credentials from a file'
    msg['From'] = 'sender@example.com'
    msg['To'] = 'receiver@example.com'
    msg.set_content('Hello from behind an authenticated relay')

    with smtplib.SMTP(host=relaying_postfix.get_container_host_ip(),
                      port=relaying_postfix.get_exposed_port(port=25)) as smtp:
        smtp.send_message(msg)

    # Mailpit rejects unauthenticated senders, so receiving the message means
    # postfix read the password out of the mounted file and used it.
    json = wait_for_message(relay_mailpit)

    assert json['total'] == 1
    assert json['messages'][0]['From']['Address'] == 'sender@example.com'

def test_lookup_table_is_readable_by_postfix_only(relaying_postfix):
    _, output = relaying_postfix.exec(
        "stat -c %U:%G:%a /etc/postfix/sasl_passwd /etc/postfix/sasl_passwd.db")

    assert output.decode().split() == ['root:postfix:640', 'root:postfix:640']

def test_password_stays_out_of_the_container_environment(relaying_postfix):
    _, output = relaying_postfix.exec("env")

    assert 'POSTMAP_sasl_passwd_FILE=/run/secrets/sasl_passwd' in output.decode()
    assert RELAY_USER.password not in output.decode()

def test_the_file_wins_when_the_variable_is_also_set(postfix_image, sasl_passwd_file):
    # The image sets POSTFIX_ and OPENDKIM_ variables of its own, so a variable
    # being set already is not by itself a mistake worth refusing to start over.
    container = DockerContainer(image=postfix_image) \
        .with_volume_mapping(str(sasl_passwd_file), '/run/secrets/sasl_passwd', 'ro') \
        .with_env('POSTMAP_sasl_passwd', 'from the environment') \
        .with_env('POSTMAP_sasl_passwd_FILE', '/run/secrets/sasl_passwd')

    container.start()

    try:
        wait_for_logs(container, "Starting the Postfix mail system", timeout=30)

        _, table = container.exec("cat /etc/postfix/sasl_passwd")

        assert table.decode().strip() == sasl_passwd_file.read_text().strip()
        assert 'POSTMAP_sasl_passwd is also set' in container.get_logs()[0].decode()
    finally:
        container.stop()

def test_a_domain_list_from_a_file_is_still_watched_by_the_health_check(
        postfix_image, tmp_path_factory):
    # The health check runs in its own process and only sees the container
    # environment, where a value read from a file never appears.
    domains = tmp_path_factory.mktemp("dkim") / "domains"
    domains.write_text("example.com\n")

    container = DockerContainer(image=postfix_image) \
        .with_volume_mapping(str(domains), '/run/secrets/dkim_domains', 'ro') \
        .with_env('OPENDKIM_DOMAINS_FILE', '/run/secrets/dkim_domains')

    container.start()

    try:
        wait_for_logs(container, "Starting the Postfix mail system", timeout=30)

        assert container.exec("/root/healthcheck")[0] == 0

        container.exec("pkill -x opendkim")

        exit_code, output = wait_for_healthcheck(container, expected=1)

        assert exit_code == 1
        assert 'relayed unsigned' in output.decode()
    finally:
        container.stop()

def test_an_srs_domain_from_a_file_is_still_watched_by_the_health_check(
        postfix_image, tmp_path_factory):
    domain = tmp_path_factory.mktemp("srs") / "domain"
    domain.write_text("relay.example.com\n")

    container = DockerContainer(image=postfix_image) \
        .with_volume_mapping(str(domain), '/run/secrets/srs_domain', 'ro') \
        .with_env('POSTSRSD_SRS_DOMAIN_FILE', '/run/secrets/srs_domain')

    container.start()

    try:
        wait_for_logs(container, "Starting the Postfix mail system", timeout=30)

        assert container.exec("/root/healthcheck")[0] == 0

        container.exec("pkill -x postsrsd")

        exit_code, output = wait_for_healthcheck(container, expected=1)

        assert exit_code == 1
        assert 'envelope senders are not rewritten' in output.decode()
    finally:
        container.stop()

def test_a_file_that_cannot_be_read_stops_the_container(postfix_image):
    container = docker.from_env().containers.run(
        postfix_image,
        detach=True,
        environment={'POSTMAP_sasl_passwd_FILE': '/run/secrets/not_mounted'})

    try:
        assert container.wait(timeout=30)['StatusCode'] == 1
        assert 'which cannot be read' in container.logs().decode()
    finally:
        container.remove(force=True)

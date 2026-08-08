"""Configuration read from files instead of from the environment.

Any POSTFIX_, POSTFIXMASTER_, POSTMAP_, OPENDKIM_ or POSTSRSD_ variable can be
suffixed with _FILE and given a path, so that the password a relay needs for
its upstream comes from a docker secret rather than from the environment,
where docker inspect and every process in the container can read it.
"""

import pytest

from testcontainers.community.mailpit import MailpitUser

from tests.helpers import container_exec, container_log, poll_until, send

UPSTREAM = '[secrets-upstream]:1025'
USER, PASSWORD = 'relay', 's3cret'
SECRET = '/run/secrets/sasl_passwd'
SASL_PASSWD = f"{UPSTREAM} {USER}:{PASSWORD}\n"


def container_stderr(container):
    """What the container refused to do: container_log only takes stdout."""
    return container.get_logs()[1].decode()


def healthcheck(container):
    """Run the image health check, which only sees the container environment."""
    return container.exec(["/root/healthcheck"])


def wait_for_unhealthy(container, timeout=15):
    """A killed daemon takes a moment to go away, and docker retries anyway."""
    def failed():
        result = healthcheck(container)
        return result if result.exit_code != 0 else None

    return poll_until(failed, timeout=timeout, description="the health check to fail")


@pytest.fixture
def upstream(mailpit_factory):
    """Server accepting only the credentials the secret file holds."""
    return mailpit_factory('secrets-upstream', users=[MailpitUser(USER, PASSWORD)])


@pytest.fixture
def relay(postfix_factory, upstream):
    """Relay set up like the "Relaying through another SMTP server" README section."""
    return postfix_factory(
        env={
            'POSTFIX_relayhost': UPSTREAM,
            'POSTFIX_smtp_sasl_auth_enable': 'yes',
            'POSTFIX_smtp_sasl_password_maps': 'hash:/etc/postfix/sasl_passwd',
            'POSTFIX_smtp_sasl_security_options': 'noanonymous',
            'POSTFIX_smtp_tls_security_level': 'encrypt',
            'POSTMAP_sasl_passwd_FILE': SECRET,
        },
        files={SECRET: SASL_PASSWD})


def test_a_password_from_a_file_relays_through_an_authenticated_upstream(relay, upstream):
    send(relay, subject='from a file')

    # The upstream refuses anyone else, so a delivered message is the proof
    # that the password reached postfix.
    assert upstream.wait_for_message('from a file')


def test_the_password_is_nowhere_in_the_container_environment(relay):
    environment = container_exec(relay, ["env"])

    assert f"POSTMAP_sasl_passwd_FILE={SECRET}" in environment
    assert PASSWORD not in environment


def test_the_generated_table_is_readable_by_postfix_only(relay):
    # postmap leaves the table and its database at 644, and they hold the
    # password that was kept out of the environment.
    stat = container_exec(relay, ["stat", "-c", "%U:%G:%a",
                                  "/etc/postfix/sasl_passwd",
                                  "/etc/postfix/sasl_passwd.db"])

    assert stat.split() == ['root:postfix:640', 'root:postfix:640']


def test_the_file_wins_when_the_variable_is_also_set(postfix_factory):
    # The image gives defaults to POSTFIX_ and OPENDKIM_ variables of its own,
    # so finding one set is not a mistake worth refusing to start over.
    relay = postfix_factory(
        env={
            'POSTMAP_sasl_passwd': 'from the environment',
            'POSTMAP_sasl_passwd_FILE': SECRET,
        },
        files={SECRET: SASL_PASSWD})

    assert container_exec(relay, ["cat", "/etc/postfix/sasl_passwd"]) == SASL_PASSWD
    assert 'POSTMAP_sasl_passwd is also set' in container_log(relay)


def test_a_path_that_cannot_be_read_stops_the_container(postfix_factory):
    relay = postfix_factory(env={'POSTMAP_sasl_passwd_FILE': '/run/secrets/not_mounted'},
                            wait_ready=False)

    assert relay.get_wrapped_container().wait(timeout=30)['StatusCode'] == 1
    assert 'which cannot be read' in container_stderr(relay)


def test_a_domain_list_from_a_file_is_watched_by_the_health_check(postfix_factory):
    relay = postfix_factory(env={'OPENDKIM_DOMAINS_FILE': '/run/secrets/dkim_domains'},
                            files={'/run/secrets/dkim_domains': "example.com\n"})

    assert healthcheck(relay).exit_code == 0

    relay.exec(["pkill", "-x", "opendkim"])

    assert 'relayed unsigned' in wait_for_unhealthy(relay).output.decode()


def test_an_srs_domain_from_a_file_is_watched_by_the_health_check(postfix_factory):
    relay = postfix_factory(env={'POSTSRSD_SRS_DOMAIN_FILE': '/run/secrets/srs_domain'},
                            files={'/run/secrets/srs_domain': "relay.example.com\n"})

    assert healthcheck(relay).exit_code == 0

    relay.exec(["pkill", "-x", "postsrsd"])

    assert 'not rewritten' in wait_for_unhealthy(relay).output.decode()

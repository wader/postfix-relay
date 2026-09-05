"""Configuration read from files instead of from the environment.

Any POSTFIX_, POSTFIXMASTER_, POSTMAP_, OPENDKIM_ or POSTSRSD_ variable can be
suffixed with _FILE and given a path, so that the password a relay needs for
its upstream comes from a docker secret rather than from the environment,
where docker inspect and every process in the container can read it.
"""

import pytest

from testcontainers.community.mailpit import MailpitUser

from tests.helpers import (container_exec, container_log, container_stderr,
                           esmtp_features, exit_code_within, healthcheck_after_stopping,
                           postconf, send)

UPSTREAM = '[secrets-upstream]:1025'
USER, PASSWORD = 'relay', 's3cret'
SECRET = '/run/secrets/sasl_passwd'
SASL_PASSWD = f"{UPSTREAM} {USER}:{PASSWORD}\n"

HOSTNAME, HOSTNAME_SECRET = 'smtp.from-a-file.test', '/run/secrets/myhostname'
SUBMISSION = 'submission inet n - y - - smtpd'
SUBMISSION_SECRET = '/run/secrets/submission'


def healthcheck(container):
    """Run the image health check, which only sees the container environment."""
    return container.exec(["/root/healthcheck"])


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


@pytest.fixture
def settings_from_files(postfix_shared):
    """Relay taking a POSTFIX_ and a POSTFIXMASTER_ setting from a file.

    Shared, because the three tests below only read it back. The other three
    prefixes are configured separately elsewhere in this file: one loop reads
    all five, but only the reading is shared -- what each of them does with the
    value afterwards is a different piece of code every time.
    """
    return postfix_shared(
        env={
            'POSTFIX_myhostname_FILE': HOSTNAME_SECRET,
            'POSTFIXMASTER_submission__inet_FILE': SUBMISSION_SECRET,
        },
        files={HOSTNAME_SECRET: f"{HOSTNAME}\n", SUBMISSION_SECRET: f"{SUBMISSION}\n"})


def test_a_password_from_a_file_relays_through_an_authenticated_upstream(relay, upstream):
    send(relay, subject='from a file')

    # The upstream refuses anyone else, so a delivered message is the proof
    # that the password reached postfix.
    assert upstream.wait_for_message('from a file')


def test_the_password_is_nowhere_in_the_container_environment(relay):
    environment = container_exec(relay, ["env"])

    assert f"POSTMAP_sasl_passwd_FILE={SECRET}" in environment
    assert PASSWORD not in environment


def test_the_generated_table_is_readable_by_root_only(relay):
    """Regression test for issue #178.

    postmap leaves the table and its database at root:root 644, and they hold
    the password that was kept out of the environment -- readable by opendkim,
    the process that parses untrusted input, and by every other uid in the
    container. 600 is what postfix's SASL_README asks for on this file.
    """
    stat = container_exec(relay, ["stat", "-c", "%U:%G:%a",
                                  "/etc/postfix/sasl_passwd",
                                  "/etc/postfix/sasl_passwd.db"])

    assert stat.split() == ['root:root:600', 'root:root:600']


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

    assert exit_code_within(relay, seconds=30) == 1
    assert 'which cannot be read' in container_stderr(relay)


def test_a_postfix_setting_from_a_file_is_what_the_relay_runs_with(settings_from_files):
    assert postconf(settings_from_files, 'myhostname') == HOSTNAME

    # Written into main.cf is not the same as used, and myhostname is a setting
    # the relay answers with: the greeting is postfix's own reading of it.
    code, banner, _ = esmtp_features(settings_from_files)
    assert code == 220
    assert banner.startswith(f"{HOSTNAME} ESMTP")


def test_a_master_cf_service_from_a_file_is_added(settings_from_files):
    """The service tests/test_config.py adds from the environment, from a file.

    Deliberately the same service and the same assertion: what differs is the
    route the value took. "__" still stands for "/", because the "_FILE" suffix
    is stripped off the name before the replacement ever sees it.
    """
    assert 'submission inet' in \
        container_exec(settings_from_files, ["postconf", "-M", "submission/inet"])


def test_the_file_variable_itself_configures_nothing(settings_from_files):
    """The unset that ends secretsFromFiles, part of CLAUDE.md invariant 15.

    Without it every loop below configures "<name>_FILE" as well, and neither
    of these two stops the container: postfix takes myhostname_FILE into
    main.cf and mentions it in a warning, and "postconf -Me" refuses
    submission/inet_FILE with a fatal the script does not see. The prefix that
    pays for it is OPENDKIM_, where one name opendkim does not know costs the
    whole configuration file -- the relay below does not come up at all
    without this unset, which is a loud failure but not one that names it.
    """
    assert 'myhostname_FILE' not in \
        container_exec(settings_from_files, ["cat", "/etc/postfix/main.cf"])
    assert 'submission/inet_FILE' not in container_stderr(settings_from_files)


def test_a_domain_list_from_a_file_is_watched_by_the_health_check(postfix_factory):
    relay = postfix_factory(env={'OPENDKIM_DOMAINS_FILE': '/run/secrets/dkim_domains'},
                            files={'/run/secrets/dkim_domains': "example.com\n"})

    assert healthcheck(relay).exit_code == 0

    assert 'relayed unsigned' in \
        healthcheck_after_stopping(relay, "opendkim").output.decode()


def test_an_srs_domain_from_a_file_is_watched_by_the_health_check(postfix_factory):
    relay = postfix_factory(env={'POSTSRSD_SRS_DOMAIN_FILE': '/run/secrets/srs_domain'},
                            files={'/run/secrets/srs_domain': "relay.example.com\n"})

    assert healthcheck(relay).exit_code == 0

    assert 'not rewritten' in \
        healthcheck_after_stopping(relay, "postsrsd").output.decode()

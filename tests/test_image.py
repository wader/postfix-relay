"""The image as it is published, before anything is started.

Everything here is read from the built image rather than from a running
relay: the defaults the README points at the Dockerfile for, the volumes,
the port, the health check, and the handful of programs the documentation
tells users to run out of the image. None of it needs a relay, so this is
the cheapest place to notice that a base image update or an edit to the
Dockerfile changed what every deployment gets.
"""

import docker
import pytest

from tests.helpers import image_config, image_run

# The environment defaults from the Dockerfile. They are the configuration of
# every container that sets nothing, and the README sends users to the
# Dockerfile to read them, so each one is part of the published interface.
DEFAULT_ENVIRONMENT = [
    # Postfix: an open relay that encrypts what it sends but not what it
    # receives, which is the trade-off the README describes.
    ('POSTFIX_myhostname', 'hostname'),
    ('POSTFIX_mydestination', 'localhost'),
    ('POSTFIX_mynetworks', '0.0.0.0/0'),
    # Pinned rather than inherited: the packaged value is written at build
    # time from the IPv6 support of the machine doing the build.
    ('POSTFIX_inet_protocols', 'ipv4'),
    ('POSTFIX_smtp_tls_security_level', 'may'),
    # The trust store ca-certificates puts in the image. Without it postfix
    # has nothing to check a server certificate against, so the two levels
    # that authenticate the next hop cannot be used at all.
    ('POSTFIX_smtp_tls_CAfile', '/etc/ssl/certs/ca-certificates.crt'),
    ('POSTFIX_smtpd_tls_security_level', 'none'),
    # OpenDKIM: everything but the domains, which is what turns signing on.
    ('OPENDKIM_Socket', 'inet:12301@localhost'),
    ('OPENDKIM_Mode', 'sv'),
    ('OPENDKIM_UMask', '002'),
    ('OPENDKIM_Syslog', 'yes'),
    ('OPENDKIM_TrustAnchorFile', '/usr/share/dns/root.key'),
    ('OPENDKIM_InternalHosts', '0.0.0.0/0, ::/0'),
    ('OPENDKIM_KeyTable', '/etc/opendkim/KeyTable'),
    ('OPENDKIM_SigningTable', 'refile:/etc/opendkim/SigningTable'),
    # rsyslog: to stdout only, without repeating the time docker already adds.
    ('RSYSLOG_TIMESTAMP', 'no'),
    ('RSYSLOG_LOG_TO_FILE', 'no'),
    # Set but empty, which is what keeps saslauthd from starting.
    ('SASL_Passwds', ''),
]

# The two directories the README says hold state that cannot be recreated.
STATEFUL_DIRECTORIES = ['/var/spool/postfix', '/etc/opendkim/keys']


@pytest.fixture(scope="module")
def image_shell(postfix_image):
    """A container that only sleeps, to ask the image about its files.

    Reading a file out of the image is a question about the image, not about
    a relay, and starting postfix to answer it would cost a start-up and a
    shutdown per question.
    """
    container = docker.from_env().containers.run(
        postfix_image, ["sleep", "300"], detach=True)

    def shell(command):
        exit_code, output = container.exec_run(["sh", "-c", command])
        return exit_code, output.decode()

    yield shell

    container.remove(force=True)


@pytest.fixture(scope="module")
def config(postfix_image):
    return image_config(postfix_image)


@pytest.fixture(scope="module")
def environment(config):
    return dict(entry.split('=', 1) for entry in config['Env'])


@pytest.mark.parametrize("name,value", DEFAULT_ENVIRONMENT,
                         ids=[name for name, _ in DEFAULT_ENVIRONMENT])
def test_the_documented_default_is_in_the_image(environment, name, value):
    """One case per default, so a changed one is named rather than counted."""
    assert environment.get(name) == value


def test_the_image_sets_no_other_variables(environment):
    """A default nobody asked for configures every deployment silently.

    PATH and DEBIAN_FRONTEND come from the base image and the build; the
    rest of the environment is the list above and nothing else.
    """
    unexpected = set(environment) - {name for name, _ in DEFAULT_ENVIRONMENT} - {'PATH'}

    assert unexpected == set()


def test_the_features_that_have_to_be_asked_for_are_off(environment):
    """DKIM, SRS and postmaster notifications are opt-in.

    Each of them is turned on by one variable being non-empty, so shipping
    any of them with a value would turn the feature on for everyone.
    """
    assert 'OPENDKIM_DOMAINS' not in environment
    assert 'POSTSRSD_SRS_DOMAIN' not in environment
    assert 'POSTMASTER_ADDRESS' not in environment
    assert 'POSTFIX_relayhost' not in environment


def test_the_image_declares_only_the_volumes_that_hold_state(config):
    """A declared volume that holds nothing still creates an anonymous one.

    The README lists these two and says everything else postfix writes is
    regenerated at start-up and deliberately not declared.
    """
    assert sorted(config['Volumes']) == sorted(STATEFUL_DIRECTORIES)


def test_the_image_exposes_the_smtp_port_only(config):
    assert list(config['ExposedPorts']) == ['25/tcp']


def test_the_image_starts_the_start_up_script(config):
    """No entrypoint, so "docker run <image> mkpasswd ..." runs mkpasswd."""
    assert config['Cmd'] == ['/root/run']
    assert not config.get('Entrypoint')


def test_the_image_ships_the_health_check(config):
    """The timings decide how long a broken relay looks fine to docker."""
    healthcheck = config['Healthcheck']

    assert healthcheck['Test'] == ['CMD', '/root/healthcheck']
    assert healthcheck['Interval'] == 30 * 10 ** 9
    assert healthcheck['Timeout'] == 5 * 10 ** 9
    assert healthcheck['StartPeriod'] == 15 * 10 ** 9
    assert healthcheck['Retries'] == 3


def test_the_two_shipped_scripts_are_executable(image_shell):
    """Both are named in the image configuration, which does not chmod them."""
    exit_code, output = image_shell("test -x /root/run && test -x /root/healthcheck && echo ok")

    assert exit_code == 0, output


def test_no_srs_secret_is_baked_into_the_image(image_shell):
    """Two deployments sharing one could forge each other's return addresses."""
    exit_code, _ = image_shell("test -e /etc/postsrsd.secret")

    assert exit_code != 0


def test_no_rsyslog_configuration_is_baked_into_the_image(image_shell):
    """"run" only generates it when it is missing, so a packaged one would
    make every RSYSLOG_ variable inert."""
    exit_code, _ = image_shell("test -e /etc/rsyslog.conf")

    assert exit_code != 0


def test_the_dkim_key_directory_is_there_and_empty(image_shell):
    """It is a declared volume: a key in the image would be shared by all."""
    exit_code, output = image_shell("ls -A /etc/opendkim/keys")

    assert exit_code == 0
    assert output.strip() == ''


def test_the_queue_holds_nothing_but_the_chroot_devices(image_shell):
    """The queue is created at start-up, its chroot support is not.

    Postfix runs its daemons chrooted in the queue directory, and the
    /dev inside it is the one rsyslog puts the chrooted log socket in.
    Nothing recreates that directory later, so it has to come from the
    image -- and mounting an empty directory over the queue hides it.
    """
    exit_code, output = image_shell("ls -A /var/spool/postfix")

    assert exit_code == 0
    assert output.split() == ['dev']


def test_mkpasswd_is_available(postfix_image):
    """The README hashes SASL passwords with "docker run --rm <image> mkpasswd"."""
    output = image_run(postfix_image, ["mkpasswd", "-m", "sha-512", "mypassword"])

    assert output.startswith('$6$')


def test_a_dkim_key_can_be_generated(image_shell):
    """opendkim-tools is what "run" generates the keys with."""
    exit_code, output = image_shell(
        "cd /tmp && opendkim-genkey --selector=sel --domain=example.com && "
        "test -s sel.private && test -s sel.txt && echo ok")

    assert exit_code == 0, output


def test_openssl_is_available(image_shell):
    """The README generates the client certificate with the image itself."""
    exit_code, output = image_shell("openssl version")

    assert exit_code == 0
    assert output.startswith('OpenSSL')


@pytest.mark.smoke
def test_postsrsd_is_installed_for_this_architecture(image_shell):
    """Debian does not build it for armhf, and "run" says so instead of
    relaying without the rewriting that was asked for. Everywhere else it
    has to be there.

    Asserted both ways round rather than skipped on armhf, so that the one
    architecture this is about is also the one it can fail on. The absence is
    what the conditional install in the Dockerfile, the guard in "run", the
    note in the README and the skip that used to be here all rest on, and a
    point release that started shipping the package would leave every one of
    them saying something untrue with nothing to notice.
    """
    _, architecture = image_shell("dpkg --print-architecture")
    _, installed = image_shell("command -v postsrsd")

    if architecture.strip() == 'armhf':
        assert installed.strip() == '', \
            "Debian now builds postsrsd for armhf, so the arm/v7 image has " \
            "SRS after all: the Dockerfile, run and the README say otherwise"
    else:
        assert installed.strip() == '/usr/sbin/postsrsd'


def test_the_certificate_authorities_are_installed(image_shell):
    """Without them, TLS to the next hop cannot be verified at all."""
    exit_code, output = image_shell("test -s /etc/ssl/certs/ca-certificates.crt && echo ok")

    assert exit_code == 0, output


def test_the_package_lists_were_cleaned_up(image_shell):
    """They are megabytes of index nothing reads at run time."""
    exit_code, output = image_shell("ls -A /var/lib/apt/lists")

    assert exit_code == 0
    assert output.strip() == ''


def test_the_image_is_a_single_debian_release(image_shell):
    """The pinned base image, which "bump" keeps up to date."""
    exit_code, output = image_shell("cat /etc/debian_version")

    assert exit_code == 0
    assert output.strip().startswith('13.'), output


def test_the_trust_store_the_ca_file_names_is_in_the_image(postfix_image):
    """The default above is only worth having if the file is there.

    ca-certificates is installed for this and nothing else: opendkim's trust
    anchor is the DNSSEC root key, not a CA bundle.
    """
    bundle = image_run(postfix_image, [
        "sh", "-c", "wc -l < /etc/ssl/certs/ca-certificates.crt"])

    assert int(bundle) > 100

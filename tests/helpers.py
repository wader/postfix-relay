import os
import re
import smtplib
import time

import docker
import requests

DEFAULT_TIMEOUT = 30

# Docker runs its DNS resolver on this address inside every container
# attached to a user defined network.
DOCKER_RESOLVER = "127.0.0.11"


def poll_until(predicate, timeout=DEFAULT_TIMEOUT, description="condition"):
    """Call predicate until it returns something truthy and return that."""
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {description}")
        time.sleep(0.2)


def once_across_workers(tmp_path_factory, name, produce, timeout=600):
    """Call "produce" once for the whole run, however many workers there are.

    pytest-xdist runs every worker in a process of its own, so a session
    scoped fixture is set up once per worker rather than once per run. That
    is what keeps the workers independent and is what one wants for the
    containers, but not for getting the images they run: the same build
    would be done several times over, and several pulls of the same base
    image at the same moment are answered by the registry with a 429 rather
    than with the image.

    The first worker to create the lock does the work and leaves a marker
    behind, the others wait for that marker. Without xdist there is nothing
    to coordinate and "produce" is simply called.
    """
    if os.environ.get('PYTEST_XDIST_WORKER') is None:
        produce()
        return

    # getbasetemp() is per worker, its parent is shared by all of them.
    shared = tmp_path_factory.getbasetemp().parent
    lock = shared / f"{name}.lock"
    done = shared / f"{name}.done"

    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        poll_until(done.is_file, timeout=timeout,
                   description=f"{name} to be made ready by another worker")
        return

    produce()
    # Written only once the work is finished, so that a worker seeing it can
    # rely on the image being there.
    done.touch()


def wait_for_smtp(container, port=25, timeout=DEFAULT_TIMEOUT):
    """Wait until the container answers on the SMTP port.

    A log line only tells that the start-up script got that far, postfix can
    still be a moment away from accepting connections.

    A container that has exited is not waited for: "run" stops the container
    on a configuration it refuses, so sitting out the timeout on one only
    delays a failure that is already decided, and reports it as a silent
    relay rather than as the refusal it is.
    """
    wrapped = container.get_wrapped_container()

    def banner():
        wrapped.reload()
        if wrapped.status == 'exited':
            raise AssertionError(
                f"the container exited with {wrapped.attrs['State']['ExitCode']} "
                f"instead of answering on port {port}")
        try:
            smtp = smtplib.SMTP(container.get_container_host_ip(),
                                container.get_exposed_port(port),
                                timeout=5)
        except (OSError, smtplib.SMTPException):
            return False
        smtp.quit()
        return True

    poll_until(banner, timeout=timeout, description=f"smtp on port {port}")


def smtp_connect(container, port=25):
    """Open an SMTP connection to a container, usable as a context manager."""
    return smtplib.SMTP(container.get_container_host_ip(),
                        container.get_exposed_port(port),
                        timeout=DEFAULT_TIMEOUT)


def send(container, port=25, sender="sender@example.com", recipients=("receiver@example.com",),
         subject="test", body="test"):
    """Send a minimal message and return the subject it was sent with."""
    message = f"Subject: {subject}\r\nFrom: {sender}\r\nTo: {', '.join(recipients)}\r\n\r\n{body}\r\n"
    with smtp_connect(container, port) as smtp:
        smtp.sendmail(sender, list(recipients), message)
    return subject


def healthcheck_after_stopping(container, daemon, stop=None, timeout=10):
    """Stop a daemon and read what the health check then says, in one exec.

    The supervision loop in "run" stops the container about a second after
    it notices a daemon is gone, and it notices within a poll interval of
    the daemon actually exiting -- which is not the same moment as the
    signal, since opendkim takes about three seconds to shut down. That
    leaves roughly two seconds between the verdict this asserts and the
    container going away.

    Polling the check from the outside spends that window on docker round
    trips, and losing it does not fail on the assertion: the next exec
    lands on a container that is gone and raises a docker error instead.
    Killing, waiting and running the check inside the container is one
    exec, issued while the container is certainly still up, so the whole
    race is the tail of a single call.

    "timeout" bounds the wait for the daemon to go. Running the check
    anyway afterwards is deliberate: a daemon that never went reports
    healthy, which fails on the assertion the caller wrote rather than on
    a timeout that says nothing about what the check would have said.
    """
    return container.exec([
        "sh", "-c",
        f'{stop or f"pkill -x {daemon}"} ; '
        f'for _ in $(seq {timeout * 10}) ; do '
        f'pgrep -x {daemon} > /dev/null || break ; sleep 0.1 ; '
        f'done ; '
        f'/root/healthcheck'])


def container_exec(container, command):
    """Run a command in the container and return its output, failing on a non zero exit."""
    result = container.exec(command)
    output = result.output.decode()
    assert result.exit_code == 0, f"{command} exited with {result.exit_code}: {output}"
    return output


def postconf(container, name):
    """Read an effective postfix setting, as postfix itself sees it."""
    return container_exec(container, ["postconf", "-h", name]).strip()


def process_running(container, name):
    """Whether a process is running in the container.

    Used to check that a daemon the user did not ask for was left alone: the
    image starts opendkim, postsrsd and saslauthd only when configured to.
    """
    return container.exec(["pgrep", "-x", name]).exit_code == 0


def esmtp_features(container, port=25):
    """The relay's greeting and what it advertises in its EHLO response.

    Connected in two steps rather than with smtp_connect: the 220 greeting is
    only returned by connect(), and it carries myhostname.
    """
    smtp = smtplib.SMTP(timeout=DEFAULT_TIMEOUT)
    code, banner = smtp.connect(container.get_container_host_ip(),
                                container.get_exposed_port(port))
    smtp.ehlo()
    features = dict(smtp.esmtp_features)
    smtp.quit()
    return code, banner.decode(), features


def dkim_dns_record(container, domain, selector):
    """The TXT record opendkim generated for a domain, as DNS would serve it.

    The generated .txt file splits the record over several quoted strings,
    which is what BIND wants; a resolver hands a verifier the concatenation.
    """
    text = container_exec(container, ["cat", f"/etc/opendkim/keys/{domain}/{selector}.txt"])
    return "".join(re.findall(r'"([^"]*)"', text))


def container_log(container):
    return container.get_logs()[0].decode()


def container_stderr(container):
    """What the container refused to do: container_log only takes stdout."""
    return container.get_logs()[1].decode()


def wait_for_log(container, text, timeout=DEFAULT_TIMEOUT):
    """Wait for a line in the container log and return the whole log."""
    return poll_until(lambda: text in container_log(container) and container_log(container),
                      timeout=timeout, description=f"{text!r} in the container log")


def wait_for_file(container, path, text, timeout=DEFAULT_TIMEOUT):
    """Wait for a file in the container to contain text and return it."""
    def content():
        result = container.exec(["cat", path])
        return result.exit_code == 0 and text in result.output.decode() and result.output.decode()

    return poll_until(content, timeout=timeout, description=f"{text!r} in {path}")


def restart(container, port=25):
    """Restart the container and wait for it to accept mail again."""
    container.get_wrapped_container().restart()
    wait_for_smtp(container, port=port)


def send_raw(container, message, sender="sender@example.com",
             recipients=("receiver@example.com",), port=25):
    """Hand a message to the relay exactly as written, envelope included.

    "send" composes the message from its parts, which is what most tests
    want; this one is for the tests that are about the bytes themselves.
    """
    with smtp_connect(container, port) as smtp:
        smtp.sendmail(sender, list(recipients), message)


def image_config(container_image):
    """The image configuration, as "docker inspect" reports it.

    What a user gets before setting anything: the declared volumes, the
    exposed port, the health check and the environment defaults the README
    points at the Dockerfile for.
    """
    return docker.from_env().images.get(container_image).attrs['Config']


def image_run(container_image, command):
    """Run a command in a throwaway container and return its output.

    The image ships tools the README tells users to run that way, mkpasswd
    for one, and it is the cheapest way to look inside it: no relay has to
    be started for a question about a file.
    """
    return docker.from_env().containers.run(
        container_image, command, remove=True).decode()


def mkpasswd(container_image, password):
    """Hash a password the way the README tells users to.

    "docker run --rm <image> mkpasswd -m sha-512 <password>", which is what
    the PAM password file the relay authenticates against is built from.
    """
    return image_run(container_image, ["mkpasswd", "-m", "sha-512", password]).strip()


def file_missing(container, path):
    """Whether a path is absent from the container."""
    return container.exec(["test", "-e", path]).exit_code != 0


def listening_ports(container):
    """The TCP ports something inside the container is listening on."""
    return {port for _, port in listening_sockets(container)}


def listening_sockets(container):
    """The (address, port) pairs something inside the container listens on.

    Read from the kernel, the way the health check does, so a daemon that
    was never asked for is noticed by the socket it opened rather than only
    by its process name.

    Docker's embedded DNS resolver listens inside every container attached
    to a user defined network; it belongs to docker rather than to the
    image, so it is left out.
    """
    sockets = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        result = container.exec(["cat", path])
        if result.exit_code != 0:
            continue
        for line in result.output.decode().splitlines()[1:]:
            fields = line.split()
            # State 0A is listening, and the local address is "address:port".
            if len(fields) < 4 or fields[3] != '0A':
                continue
            address, port = fields[1].split(':')
            address = _dotted_quad(address) or address
            if address == DOCKER_RESOLVER:
                continue
            sockets.add((address, int(port, 16)))
    return sockets


def _dotted_quad(hex_address):
    """"0B00007F" -> "127.0.0.11", the byte order /proc/net/tcp writes."""
    if len(hex_address) != 8:
        return None
    return '.'.join(str(int(hex_address[index:index + 2], 16))
                    for index in range(6, -2, -2))


def exit_code_within(container, seconds=15):
    """The code the container exited with, or None if it is still running.

    docker's wait endpoint has no notion of "not yet": asking it to wait
    less than the container takes raises a read timeout, which is the
    answer rather than an error here.

    Fifteen seconds because that is what the supervision actually needs.
    rsyslogd is a job of the start-up script and its death is seen at once,
    but the other four daemons are noticed by the polling loop, which has to
    come round and then confirm the reading. Measured on the image as it
    stands, from the kill to the container being gone: rsyslogd 1.3s,
    postsrsd and saslauthd 3.1s, the postfix master 3.1s, opendkim 7.1s. The
    bound is a little over twice the slowest of those, and it costs nothing
    when the container does stop, which is the outcome its caller asserts.
    """
    try:
        return container.get_wrapped_container().wait(timeout=seconds)['StatusCode']
    except requests.exceptions.RequestException:
        return None

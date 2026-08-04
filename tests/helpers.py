import smtplib
import time

DEFAULT_TIMEOUT = 30


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


def wait_for_smtp(container, port=25, timeout=DEFAULT_TIMEOUT):
    """Wait until the container answers on the SMTP port.

    A log line only tells that the start-up script got that far, postfix can
    still be a moment away from accepting connections.
    """
    def banner():
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


def container_exec(container, command):
    """Run a command in the container and return its output, failing on a non zero exit."""
    result = container.exec(command)
    output = result.output.decode()
    assert result.exit_code == 0, f"{command} exited with {result.exit_code}: {output}"
    return output


def postconf(container, name):
    """Read an effective postfix setting, as postfix itself sees it."""
    return container_exec(container, ["postconf", "-h", name]).strip()


def container_log(container):
    return container.get_logs()[0].decode()


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

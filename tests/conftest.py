import pytest

pytest_plugins = [
   "tests.fixtures.mailpit",
   "tests.fixtures.postfix",
   "tests.fixtures.shared_network",
   "tests.fixtures.smtp",
]


def print_log_on_failure(request, name, container):
    """Print a container log when the test that used it failed.

    A failure is otherwise just a missing mail: the reason why postfix did
    not relay it is only in the container log, which testcontainers throws
    away together with the container. Fixtures that stop containers have to
    call this before doing so.
    """
    report = getattr(request.node, "report_call", None)
    if report is None or not report.failed:
        return
    stdout, stderr = container.get_logs()
    print(f"----- {name} log -----")
    print(stdout.decode())
    # "run" reports what stopped it on stderr, which is all a container that
    # refused to start has to say.
    if stderr:
        print(f"----- {name} stderr -----")
        print(stderr.decode())


@pytest.fixture(autouse=True)
def shared_container_logs(request):
    """Show the log of the containers shared by the tests when one fails."""
    shared = []
    for name in ("postfix", "mailpit_container"):
        if name in request.fixturenames:
            shared.append((name, request.getfixturevalue(name)))

    yield

    for name, container in shared:
        print_log_on_failure(request, name, container)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    setattr(item, f"report_{report.when}", report)
    return report

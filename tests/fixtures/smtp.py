import pytest

from tests.helpers import smtp_connect


@pytest.fixture
def smtp(postfix):
    """Client connected to the relay shared by the tests.

    Function scoped: a test that leaves the connection in a broken state,
    which the ones about rejected mail do, must not affect the next one.
    """
    smtp = smtp_connect(postfix)
    yield smtp
    smtp.close()

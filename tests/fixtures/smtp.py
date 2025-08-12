import pytest
import smtplib

@pytest.fixture(scope="session")
def smtp(postfix):
    smtp = smtplib.SMTP(host=postfix.get_container_host_ip(), port=postfix.get_exposed_port(port=25))
    yield smtp
    smtp.close()

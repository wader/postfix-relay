"""qshape is the queue-inspection tool the postfix package ships.

It is a perl script needing File::Find, and the postfix package names perl in
no dependency of its own, so nothing in it guarantees the tool can run. It runs
here only because opendkim-tools pulls perl in; the Dockerfile names perl
explicitly so that stays true whatever opendkim does, and this is the test that
would notice if it stopped being.
"""

from tests.helpers import container_exec


def test_qshape_reports_on_the_queues(postfix):
    output = container_exec(postfix, ["qshape"])

    # The header row of an empty deferred queue summary.
    assert "TOTAL" in output

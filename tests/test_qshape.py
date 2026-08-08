"""qshape is the queue-inspection tool the postfix package ships.

It is a perl script that needs File::Find from perl-modules, which
--no-install-recommends used to keep out of the image, so it died on start
with "Can't locate File/Find.pm". The perl package is installed explicitly
for it now.
"""

from tests.helpers import container_exec


def test_qshape_reports_on_the_queues(postfix):
    output = container_exec(postfix, ["qshape"])

    # The header row of an empty deferred queue summary.
    assert "TOTAL" in output

"""Turning environment variables into postfix configuration.

The image is configured through POSTFIX_, POSTFIXMASTER_ and POSTMAP_
variables, so the mapping from a variable to what postfix ends up doing is
the part of "run" users depend on the most.
"""

import smtplib

import pytest

from tests.helpers import (container_exec, container_log, esmtp_features,
                           listening_ports, postconf, send, smtp_connect, wait_for_log)


def test_postfix_variables_configure_main_cf(postfix_factory, mailpit):
    relay = postfix_factory(env={
        'POSTFIX_message_size_limit': '2000',
        'POSTFIX_smtpd_recipient_restrictions': 'permit_mynetworks, reject_unauth_destination',
    })

    assert postconf(relay, 'message_size_limit') == '2000'
    # Values with spaces and commas have to survive the round trip.
    assert postconf(relay, 'smtpd_recipient_restrictions') == \
        'permit_mynetworks, reject_unauth_destination'

    # And the setting is not just stored, postfix enforces it.
    with pytest.raises(smtplib.SMTPSenderRefused) as rejected:
        send(relay, subject='too big', body='x' * 5000)
    assert rejected.value.smtp_code == 552

    send(relay, subject='small enough')
    assert mailpit.wait_for_message('small enough')


def test_myhostname_is_what_the_relay_calls_itself(postfix_factory, mailpit):
    """The one setting the README tells every user to set."""
    relay = postfix_factory(env={
        'POSTFIX_myhostname': 'smtp.example.test',
        # An empty value clears a Dockerfile default instead of being skipped,
        # which is the only way to turn one of them off.
        'POSTFIX_smtp_tls_security_level': '',
    })

    code, banner, _ = esmtp_features(relay)
    assert code == 220
    assert banner.startswith('smtp.example.test ESMTP')
    assert postconf(relay, 'smtp_tls_security_level') == ''

    send(relay, subject='named relay')

    assert any('by smtp.example.test (Postfix)' in received
               for received in mailpit.wait_for_message('named relay')['headers']['received'])


def test_postfixmaster_variables_configure_master_cf(postfix_factory, mailpit):
    """A POSTFIXMASTER_ variable adds a service, "__" standing for "/"."""
    relay = postfix_factory(
        env={'POSTFIXMASTER_submission__inet': 'submission inet n - y - - smtpd'},
        ports=(25, 587),
    )

    assert 'submission inet' in container_exec(relay, ["postconf", "-M", "submission/inet"])

    send(relay, port=587, subject='through submission')

    assert mailpit.wait_for_message('through submission')


def test_postfixmaster_variables_leave_nothing_in_main_cf(postfix_factory):
    """The two loops in "run" are two only because "${!POSTFIX_*}" keeps its
    trailing underscore.

    Bash matches variable names by literal prefix, so POSTFIX_ demands "_"
    where POSTFIXMASTER_ has "M". Drop that one character -- the sort of edit
    a tidy-up makes -- and every POSTFIXMASTER_ variable goes to "postconf -e"
    as well, landing the master.cf entry's value in main.cf under a mangled
    name while the service is still added correctly and the relay still comes
    up healthy. Measured on the built image with "run" mounted that way:
    "ASTER_submission__inet = submission inet n - y - - smtpd" in main.cf, one
    postconf warning, and every other assertion in this file still passing.

    What makes it worth an assertion rather than a comment is where the value
    lands: a POSTFIXMASTER_<name>_FILE secret is resolved before these loops
    (invariant 17), so the same edit would write a resolved credential into
    main.cf. (issue #314)
    """
    relay = postfix_factory(
        env={'POSTFIXMASTER_submission__inet': 'submission inet n - y - - smtpd'},
        ports=(25, 587),
    )

    main_cf = container_exec(relay, ["cat", "/etc/postfix/main.cf"])

    assert 'submission__inet' not in main_cf, (
        "a POSTFIXMASTER_ variable reached main.cf: the POSTFIX_ loop matched it too")
    assert 'unused parameter' not in container_log(relay)


def test_postmap_variables_create_indexed_tables(postfix_factory, mailpit):
    """POSTMAP_<file> writes /etc/postfix/<file> and runs postmap on it."""
    table = ("routed.example relay:[mailpit]:1025\n"
             "other.example relay:[mailpit]:1025")

    relay = postfix_factory(env={
        # Without a relayhost, only the transport map can route this mail.
        'POSTFIX_relayhost': '',
        'POSTFIX_transport_maps': 'hash:/etc/postfix/transport',
        'POSTMAP_transport': table,
    })

    assert container_exec(relay, ["cat", "/etc/postfix/transport"]).strip() == table
    assert container_exec(
        relay, ["postmap", "-q", "routed.example", "hash:/etc/postfix/transport"]
    ).strip() == "relay:[mailpit]:1025"

    send(relay, recipients=('receiver@routed.example',), subject='transported')

    assert mailpit.wait_for_message('transported')


def test_mynetworks_restricts_who_may_relay(postfix_factory, mailpit):
    """The default open relay can be closed down with POSTFIX_mynetworks.

    The image is an open relay by default and only docker networking keeps
    it safe, which is what issues #16, #20 and #107 are all about.
    """
    relay = postfix_factory(env={'POSTFIX_mynetworks': '127.0.0.0/8'})

    with smtp_connect(relay) as smtp:
        with pytest.raises(smtplib.SMTPRecipientsRefused) as rejected:
            smtp.sendmail('sender@example.com', ['receiver@example.com'],
                          'Subject: denied\r\n\r\nbody\r\n')

    code, message = rejected.value.recipients['receiver@example.com']
    assert code == 454
    assert b'Relay access denied' in message

    mailpit.assert_nothing_delivered()


# Two tables and the settings that use them, in one relay: writing the file,
# indexing it and reading it back are the three things POSTMAP_ has to do.
TABLES = {
    'POSTFIX_relayhost': '',
    'POSTFIX_transport_maps': 'hash:/etc/postfix/transport',
    'POSTMAP_transport': ("# where each domain goes\n"
                          "routed.example relay:[mailpit]:1025\n"
                          "\n"
                          "other.example relay:[mailpit]:1025"),
    'POSTFIX_header_checks': 'regexp:/etc/postfix/header_checks',
    'POSTMAP_header_checks': '/^X-Internal-Note:/ IGNORE',
}

# A submission port with restrictions of its own, which is the reason to add
# a service rather than to change the one on 25.
SUBMISSION = {
    'POSTFIXMASTER_submission__inet':
        'submission inet n - y - - smtpd -o smtpd_client_restrictions=reject',
}


@pytest.fixture
def tables(postfix_shared):
    return postfix_shared(env=TABLES)


@pytest.fixture
def submission(postfix_shared):
    return postfix_shared(env=SUBMISSION, ports=(25, 587))


def test_a_table_is_written_exactly_as_it_was_given(tables):
    """Comments and blank lines included: a table is edited by whoever wrote
    it, and a script that reformatted it would fight every edit."""
    written = container_exec(tables, ["cat", "/etc/postfix/transport"])

    assert written == TABLES['POSTMAP_transport'] + "\n"


def test_every_table_is_indexed(tables):
    """postmap is run on each of them, which is what "hash:" needs."""
    for name in ('transport', 'header_checks'):
        assert tables.exec(["test", "-s", f"/etc/postfix/{name}.db"]).exit_code == 0, name

    assert container_exec(tables, ["postmap", "-q", "routed.example",
                                   "hash:/etc/postfix/transport"]).strip() == \
        'relay:[mailpit]:1025'


def test_a_second_table_is_written_too(tables):
    """One POSTMAP_ variable per file, and they do not overwrite each other."""
    assert container_exec(tables, ["cat", "/etc/postfix/header_checks"]).strip() == \
        TABLES['POSTMAP_header_checks']


def test_header_checks_read_the_table_that_was_written(tables, mailpit):
    """The table is not only on disk, postfix acts on it."""
    send(tables, recipients=('receiver@routed.example',), subject='checked headers',
         body='body')

    message = mailpit.wait_for_message('checked headers')

    assert 'x-internal-note' not in message['headers']


def test_every_domain_the_table_names_is_routed(tables, mailpit):
    """Both entries, not only the first one the file happens to have."""
    send(tables, recipients=('receiver@other.example',), subject='second domain')

    assert mailpit.wait_for_message('second domain')


def test_a_domain_the_table_does_not_name_is_not_relayed(tables, mailpit):
    """With no relayhost, the table is the only route there is.

    Which is what makes a transport map a way of deciding where mail goes
    rather than a shortcut on top of one destination for everything.
    """
    send(tables, recipients=('receiver@unrouted.example',), subject='no route')

    log = wait_for_log(tables, 'unrouted.example')

    assert 'status=bounced' in log
    assert 'Host or domain name not found' in log
    assert 'no route' not in [summary['Subject'] for summary in mailpit.summaries()]


def test_a_service_added_to_master_cf_carries_its_own_options(submission, mailpit):
    """The point of adding a service rather than changing the one on 25:
    a submission port can be locked down while the internal one is not."""
    assert '-o smtpd_client_restrictions=reject' in \
        container_exec(submission, ["postconf", "-M", "submission/inet"])

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        send(submission, port=587, subject='refused on submission')

    # ...and the service on 25 is untouched by the option.
    send(submission, subject='accepted on 25')

    assert mailpit.wait_for_message('accepted on 25')


def test_both_ports_are_answered(submission):
    """A service that was added has to be there, not only in master.cf."""
    assert listening_ports(submission) == {25, 587}


def test_a_value_may_contain_a_dollar_sign(postfix_shared):
    """Postfix expands $names itself, so the script has to pass them through.

    A shell that expanded them first would turn a banner into an empty
    string and a myhostname reference into nothing at all.
    """
    relay = postfix_shared(env={'POSTFIX_smtpd_banner': '$myhostname ESMTP (relay)'})

    assert postconf(relay, 'smtpd_banner') == '$myhostname ESMTP (relay)'

    code, banner, _ = esmtp_features(relay)

    assert code == 220
    assert banner.startswith('hostname ESMTP (relay)')


def test_a_parameter_postfix_does_not_know_does_not_stop_the_relay(postfix_shared, mailpit):
    """A typo in a variable name must not cost the mail.

    postconf writes whatever it is given, so the value ends up in main.cf
    and postfix reports it as unused rather than refusing to start.
    """
    relay = postfix_shared(env={'POSTFIX_no_such_postfix_parameter': 'whatever'})

    assert container_exec(relay, ["grep", "no_such_postfix_parameter",
                                  "/etc/postfix/main.cf"]).strip() == \
        'no_such_postfix_parameter = whatever'

    send(relay, subject='relayed anyway')

    assert mailpit.wait_for_message('relayed anyway')


def test_ipv6_can_be_turned_on(postfix_shared):
    """The README says the image keeps Debian's ipv4 only setting and how to
    change it, which is only useful if changing it opens the socket."""
    relay = postfix_shared(env={'POSTFIX_inet_protocols': 'all'})

    assert postconf(relay, 'inet_protocols') == 'all'

    if relay.exec(["test", "-r", "/proc/net/tcp6"]).exit_code != 0:
        pytest.skip("this docker network has no IPv6 stack to listen on")

    assert 25 in listening_ports(relay)


def test_a_client_restriction_reaches_smtpd(postfix_shared, mailpit):
    """The restrictions are how the README says to close the relay down."""
    relay = postfix_shared(env={'POSTFIX_smtpd_client_restrictions': 'reject'})

    with pytest.raises(smtplib.SMTPRecipientsRefused) as rejected:
        send(relay, subject='rejected client')

    assert rejected.value.recipients['receiver@example.com'][0] == 554
    mailpit.assert_nothing_delivered()

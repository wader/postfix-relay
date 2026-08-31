<div id="top"></div>

# postfix-relay

Postfix SMTP relay docker image. Useful for sending email without using an
external SMTP server.

Default configuration is an open relay that relies on docker networking for
protection. So be careful to not expose it publicly, see
[Securing the relay](#securing-the-relay).

## Table of contents
<ol>
  <li><a href="#supported-architectures">Supported architectures</a></li>
  <li><a href="#quick-start">Quick start</a></li>
  <li>
    <a href="#configuration">Configuration</a>
    <ul>
      <li><a href="#postfix-variables">Postfix variables</a></li>
      <li><a href="#postfix-mastercf-variables">Postfix master.cf variables</a></li>
      <li><a href="#postfix-lookup-tables">Postfix lookup tables</a></li>
      <li><a href="#opendkim-variables">OpenDKIM variables</a></li>
      <li><a href="#postsrsd-variables">PostSRSd variables</a></li>
      <li><a href="#postmaster-notifications">Postmaster notifications</a></li>
      <li><a href="#timezone">Timezone</a></li>
    </ul>
  </li>
  <li>
    <a href="#securing-the-relay">Securing the relay</a>
    <ul>
      <li><a href="#client-authentication">Client authentication</a></li>
      <li><a href="#what-runs-as-root-and-what-to-take-away">What runs as root, and what to take away</a></li>
    </ul>
  </li>
  <li>
    <a href="#spf-and-dkim">SPF and DKIM</a>
    <ul>
      <li><a href="#spf">SPF</a></li>
      <li><a href="#dkim">DKIM</a></li>
    </ul>
  </li>
  <li><a href="#volumes">Volumes</a></li>
  <li>
    <a href="#logging">Logging</a>
    <ul>
      <li><a href="#advanced-logging-configuration">Advanced logging configuration</a></li>
    </ul>
  </li>
  <li><a href="#health-check">Health check</a></li>
  <li><a href="#troubleshooting">Troubleshooting</a></li>
  <li><a href="#testing">Testing</a></li>
  <li><a href="#license">License</a></li>
</ol>

<!-- SUPPORTED ARCHITECTURES -->
## Supported architectures

The Docker hub image is built for the following CPU architectures:

- `amd64`
- `arm/v7`
- `arm64`

Note that [SRS rewriting](#postsrsd-variables) is unavailable on `arm/v7`.

<p align="right">(<a href="#top">back to top</a>)</p>

<!-- QUICK START -->
## Quick start

`docker pull mwader/postfix-relay` or clone/build it yourself.

You probably want to set `POSTFIX_myhostname` (the FQDN used by 220/HELO), see
[Postfix variables](#postfix-variables).

### Using docker run
```
docker run -e POSTFIX_myhostname=smtp.domain.tld mwader/postfix-relay
```

### Using docker-compose
```
app:
  # use hostname "smtp" as SMTP server

smtp:
  image: mwader/postfix-relay
  restart: always
  environment:
    - POSTFIX_myhostname=smtp.domain.tld
    - OPENDKIM_DOMAINS=smtp.domain.tld
```

<p align="right">(<a href="#top">back to top</a>)</p>

<!-- CONFIGURATION -->
## Configuration

Everything is configured with environment variables, one prefix per daemon, plus
a few files to mount. See [Dockerfile](Dockerfile) for the defaults.

### Postfix variables

Postfix [configuration options](http://www.postfix.org/postconf.5.html) can be set
using `POSTFIX_<name>` environment variables. See [Dockerfile](Dockerfile) for default
configuration. You probably want to set `POSTFIX_myhostname` (the FQDN used by 220/HELO).

Note that `POSTFIX_myhostname` will change the postfix option
[myhostname](http://www.postfix.org/postconf.5.html#myhostname). The image ships
`POSTFIX_myhostname=hostname`, so unless you set it yourself a running container
ends up with the literal string `hostname` rather than the qualified name
postfix would otherwise derive from `gethostname()`. Set it to the FQDN clients
and remote servers should see: it is used for the 220 greeting and HELO, and
[myorigin](http://www.postfix.org/postconf.5.html#myorigin) derives from it, so
it also affects `Received` headers and the envelope sender of mail postfix
generates itself.

The image keeps Debian's `inet_protocols = ipv4`, so it neither accepts
connections nor delivers mail over IPv6. Set `POSTFIX_inet_protocols=all` if
your docker network has IPv6, or recipients you send to are IPv6 only.

### Postfix master.cf variables

You can modify master.cf using postconf with `POSTFIXMASTER_` variables. All double `__` symbols will be replaced with `/`. For example

```
- POSTFIXMASTER_submission__inet=submission inet n - y - - smtpd
```
will produce

```
postconf -Me submission/inet="submission inet n - y - - smtpd"
```

### Postfix lookup tables

You can also create multiline [tables](http://www.postfix.org/DATABASE_README.html#types) using `POSTMAP_<filename>` like this example:
```
environment:
  - POSTFIX_transport_maps=hash:/etc/postfix/transport
  - |
    POSTMAP_transport=gmail.com smtp
    mydomain.com relay:[relay1.mydomain.com]:587
    * relay:[relay2.mydomain.com]:587
```
which will generate file `/etc/postfix/transport`
```
gmail.com smtp
mydomain.com relay:[relay1.mydomain.com]:587
* relay:[relay2.mydomain.com]:587
```
and run `postmap /etc/postfix/transport`.

### OpenDKIM variables

OpenDKIM [configuration options](http://opendkim.org/opendkim.conf.5.html) can be set
using `OPENDKIM_<name>` environment variables. See [Dockerfile](Dockerfile) for default
configuration. For example `OPENDKIM_Canonicalization=relaxed/simple`.

Enabling signing itself is described in [DKIM](#dkim).

### PostSRSd variables

[SRS](https://en.wikipedia.org/wiki/Sender_Rewriting_Scheme) rewriting is off by
default. Set `POSTSRSD_SRS_DOMAIN` to the domain envelope senders should be
rewritten to, and the container starts [PostSRSd](https://github.com/roehling/postsrsd)
and points postfix at it:

```
environment:
  - POSTSRSD_SRS_DOMAIN=smtp.domain.tld
```

Any other setting from `/etc/default/postsrsd` can be set the same way, using
`POSTSRSD_<name>` environment variables, for example
`POSTSRSD_SRS_EXCLUDE_DOMAINS=.domain.tld,otherdomain.tld`.

Only the envelope sender is rewritten (`sender_canonical_classes=envelope_sender`),
so the visible `From:` header is left alone. If you set any of
`POSTFIX_sender_canonical_maps`, `POSTFIX_sender_canonical_classes`,
`POSTFIX_recipient_canonical_maps` or `POSTFIX_recipient_canonical_classes`
yourself, your value is used instead.

Rewritten addresses are signed with a secret in `/etc/postsrsd.secret`. The image
ships without one, and a random secret is generated on first start, so no two
deployments share a key. Return addresses stay valid for 21 days, so mount the
file if you want them to survive recreating the container:

```
volumes:
  - /your_local_path/postsrsd.secret:/etc/postsrsd.secret
```

Note that Debian does not build postsrsd for armhf in trixie, so SRS is unavailable on the `arm/v7` image. Setting `POSTSRSD_SRS_DOMAIN` there stops the container with an explicit error rather than relaying without the rewriting you asked for.

### Postmaster notifications

Postfix reports problems with itself by mailing the postmaster — the queue
filling up, a daemon that keeps crashing. Which problems are reported is
controlled by [notify_classes](https://www.postfix.org/postconf.5.html#notify_classes),
`resource, software` by default.

Those notices are addressed to the unqualified `postmaster`, which postfix
qualifies with the relay's own hostname. A relay accepts no mail for itself, so
out of the box they are deferred until they time out and are then dropped: no
one is told that anything is wrong. Set `POSTMASTER_ADDRESS` to a mailbox
someone reads:

```
environment:
  - POSTMASTER_ADDRESS=ops@domain.tld
```

This points [error_notice_recipient](https://www.postfix.org/postconf.5.html#error_notice_recipient),
`bounce_notice_recipient`, `2bounce_notice_recipient` and
`delay_notice_recipient` at that address. If you set any of those yourself with
the matching `POSTFIX_<name>` variable, your value is used instead.

Setting all four does not change how much mail you get: `notify_classes` keeps
its default, and the bounce, 2bounce and delay recipients are only used if you
widen it with `POSTFIX_notify_classes`. They are set anyway so that they are
already correct if you do.

Set `POSTFIX_myhostname` as well, or the notices may still be refused. They are
sent *from*
[double_bounce_sender](https://www.postfix.org/postconf.5.html#double_bounce_sender)
qualified with `myorigin`, which derives from `myhostname` — so with the default
they come from `double-bounce@hostname`, a domain that does not resolve, and a
receiver that rejects unknown sender domains will turn them away however good
the recipient address is.

### Timezone
Wrong timestamps in log can be fixed by setting proper timezone.
This parameter is handled by Debian base image.

```
environment:
  ...
  - TZ=Europe/Prague
```

<p align="right">(<a href="#top">back to top</a>)</p>

<!-- SECURING THE RELAY -->
## Securing the relay

The default configuration is an open relay that relies on docker networking for
protection: anything that can reach port 25 can send mail through it, to
anyone. That is fine while the port is only reachable from other containers on
the same docker network, and it is why the port should not be published unless
something outside docker really has to reach it.

If it does, stop relaying for the whole world first, either by restricting who
may relay by address:

```
environment:
  # Whatever your docker network actually is
  - POSTFIX_mynetworks=127.0.0.0/8,172.16.0.0/12
```

or by requiring authentication, using the setup described below and refusing
everyone else:

```
environment:
  - POSTFIX_smtpd_relay_restrictions=permit_sasl_authenticated,reject
```

Clients that authenticate should also be able to do it over an encrypted
connection, otherwise the password crosses the network in the clear. Mount a
certificate and its key, and point postfix at them:

```
volumes:
  - /your_local_path/cert.pem:/etc/postfix/tls/cert.pem:ro
  - /your_local_path/key.pem:/etc/postfix/tls/key.pem:ro
environment:
  - POSTFIX_smtpd_tls_cert_file=/etc/postfix/tls/cert.pem
  - POSTFIX_smtpd_tls_key_file=/etc/postfix/tls/key.pem
  # "may" advertises STARTTLS, "encrypt" refuses clients that do not use it
  - POSTFIX_smtpd_tls_security_level=may
  # Never accept credentials over an unencrypted connection
  - POSTFIX_smtpd_tls_auth_only=yes
```

Mail leaving the relay is already sent over TLS whenever the receiving server
offers it (`POSTFIX_smtp_tls_security_level=may`). Set it to `encrypt` when
relaying through a provider, where an unencrypted connection is a
misconfiguration rather than the only option.

### Client authentication
The container includes [Postfix SASL](https://www.postfix.org/SASL_README.html) authentication options that are disabled by default.

#### Example basic client PAM auth
First, create a passwd file.

```
echo "myuser:"`docker run --rm mwader/postfix-relay mkpasswd -m sha-512 "mypassword"` >> passwd_file
```

Then mount the passwd file and add the following postfix configs via enviromental variable.

```
volumes:
  - /path/to/passwd_file:/etc/postfix/sasl/sasl_passwds
environment:
  - SASL_Passwds=/etc/postfix/sasl/sasl_passwds
  - POSTFIX_smtpd_sasl_auth_enable=yes
  - POSTFIX_cyrus_sasl_config_path=/etc/postfix/sasl
  - POSTFIX_smtpd_sasl_security_options=noanonymous
  - POSTFIX_smtpd_relay_restrictions=permit_sasl_authenticated,reject
```

### What runs as root, and what to take away

The container starts as root and cannot do otherwise: postfix refuses to run as
anyone else (`the postfix command is reserved for the superuser`). It binds
port 25, sets up its chroots and then drops privileges by itself, so what
actually handles mail is not root:

| Process | Runs as |
| --- | --- |
| the start-up script, `master`, `rsyslogd` | `root` |
| `smtpd`, `cleanup`, `qmgr`, `smtp`, the rest of postfix | `postfix`, chrooted into `/var/spool/postfix` |
| `opendkim` | `opendkim` |
| `postsrsd` | `postsrsd`, chrooted into `/var/lib/postsrsd` |

The root processes supervise and log; they do not parse untrusted input. Most
of what docker grants the container by default is therefore unused and can be
taken away:

```
cap_drop:
  - ALL
cap_add:
  - CHOWN            # hand the queue to postfix and the DKIM keys to opendkim
  - DAC_OVERRIDE     # read them back afterwards
  - FOWNER           # set their modes
  - NET_BIND_SERVICE # port 25, where docker does not already allow it
  - SETGID           # the daemons dropping privileges
  - SETUID
  - SYS_CHROOT       # the jails they drop into
security_opt:
  - no-new-privileges
```

That leaves `AUDIT_WRITE`, `FSETID`, `KILL`, `MKNOD`, `NET_RAW`, `SETFCAP` and
`SETPCAP` dropped, `NET_RAW` — raw sockets, and the packet spoofing that comes
with them — being the one worth the trouble on a container that listens on a
network. Relaying, signing, rewriting, the health check and a graceful stop all
work with the set above, and a test checks that they do.

Under `docker stack deploy` the `security_opt` line is dropped — swarm prints
`Ignoring unsupported options: security_opt` and the container starts without
`no-new-privileges`. The capability set above is passed through unchanged.

<p align="right">(<a href="#top">back to top</a>)</p>

<!-- SPF AND DKIM -->
## SPF and DKIM

### SPF
When sending email using your own SMTP server it is probably a good idea
to setup [SPF](https://en.wikipedia.org/wiki/Sender_Policy_Framework) for the
domain you're sending from.

### DKIM
To enable [DKIM](https://en.wikipedia.org/wiki/DomainKeys_Identified_Mail),
specify a whitespace-separated list of domains in the environment variable
`OPENDKIM_DOMAINS`. The default DKIM selector is "mail", but can be changed to
"`<selector>`" using the syntax `OPENDKIM_DOMAINS=<domain>=<selector>`.

At container start, RSA key pairs will be generated for each domain unless the
file `/etc/opendkim/keys/<domain>/<selector>.private` exists. If you want the
keys to persist indefinitely, make sure to mount a volume for
`/etc/opendkim/keys`, otherwise they will be destroyed when the container is
removed.

DNS records to configure can be found in the container log or by running `docker exec <container> sh -c 'cat /etc/opendkim/keys/*/*.txt` you should see something like this:
```bash
$ docker exec 7996454b5fca sh -c 'cat /etc/opendkim/keys/*/*.txt'

mail._domainkey.smtp.domain.tld. IN	TXT	( "v=DKIM1; h=sha256; k=rsa; "
	  "p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0Dx7wLGPFVaxVQ4TGym/eF89aQ8oMxS9v5BCc26Hij91t2Ci8Fl12DHNVqZoIPGm+9tTIoDVDFEFrlPhMOZl8i4jU9pcFjjaIISaV2+qTa8uV1j3MyByogG8pu4o5Ill7zaySYFsYB++cHJ9pjbFSC42dddCYMfuVgrBsLNrvEi3dLDMjJF5l92Uu8YeswFe26PuHX3Avr261n"
	  "j5joTnYwat4387VEUyGUnZ0aZxCERi+ndXv2/wMJ0tizq+a9+EgqIb+7lkUc2XciQPNuTujM25GhrQBEKznvHyPA6fHsFheymOuB763QpkmnQQLCxyLygAY9mE/5RY+5Q6J9oDOQIDAQAB" )  ; ----- DKIM key mail for smtp.domain.tld
```

Other OpenDKIM options are set with the `OPENDKIM_<name>` variables described in
[OpenDKIM variables](#opendkim-variables).

<p align="right">(<a href="#top">back to top</a>)</p>

<!-- VOLUMES -->
## Volumes
The image declares volumes for the two directories holding state that cannot be
recreated:

- `/var/spool/postfix` — the postfix queue. Mail that has been accepted but not
  yet delivered lives here, so replacing a container while messages are queued
  loses them unless the queue is persisted.
- `/etc/opendkim/keys` — the DKIM private keys. If they are lost, new keys are
  generated at the next start and the DNS records you published no longer match
  (see [DKIM](#dkim)).

Everything else postfix writes is regenerated at start-up and is deliberately
not declared. `/var/lib/postfix` only holds a lock file and the TLS PRNG seed,
and `/var/mail` only receives mail addressed to a local user, which is not what
a relay is for.

Note that declaring a volume is not by itself enough to preserve anything.
Without an explicit mount docker creates an *anonymous* volume: `docker compose
up` carries it over when it recreates a container, but a plain `docker rm` and
`docker run` replaces it with an empty one. To keep the queue and the keys
across container replacement, mount them yourself:

```
volumes:
  - /your_local_path/spool:/var/spool/postfix
  - /your_local_path/dkim-keys:/etc/opendkim/keys
```

Do not point two running containers at the same queue directory. Postfix's
singleton lock lives in `/var/lib/postfix` rather than in the queue, so nothing
prevents two masters from working on one queue and duplicating or corrupting
mail.

<p align="right">(<a href="#top">back to top</a>)</p>

<!-- LOGGING -->
## Logging
By default container only logs to stdout. If you also wish to log `mail.*` messages to file on persistent volume, you can do something like:

```
environment:
  ...
  - RSYSLOG_LOG_TO_FILE=yes
  - RSYSLOG_TIMESTAMP=yes
volumes:
  - /your_local_path:/var/log/
```

You can also forward log output to remote syslog server if you define `RSYSLOG_REMOTE_HOST` variable. It always uses UDP protocol and port `514` as default value,
port number can be changed to different one with `RSYSLOG_REMOTE_PORT`. Default format of forwarded messages is defined by Rsyslog template `RSYSLOG_ForwardFormat`,
you can change it to [another template](https://www.rsyslog.com/doc/v8-stable/configuration/templates.html) (section Reserved Template Names) if you wish with `RSYSLOG_REMOTE_TEMPLATE` variable.

```
environment:
  ...
  - RSYSLOG_REMOTE_HOST=my.remote-syslog-server.com
  - RSYSLOG_REMOTE_PORT=514
  - RSYSLOG_REMOTE_TEMPLATE=RSYSLOG_ForwardFormat
```

### Advanced logging configuration

If configuration via environment variables is not flexible enough it's possible to configure rsyslog directly: `.conf` files in the `/etc/rsyslog.d` directory will be [sorted alphabetically](https://www.rsyslog.com/doc/v8-stable/rainerscript/include.html#file) and included into the primary configuration.

<p align="right">(<a href="#top">back to top</a>)</p>

<!-- HEALTH CHECK -->
## Health check

The image ships a `HEALTHCHECK`, so `docker ps` reports whether the relay is
actually able to work. It covers every daemon the container started, not only
the postfix master, because a relay that has lost OpenDKIM keeps accepting mail
and sends it unsigned:

- postfix is running and listening on every `inet` service in `master.cf`, so a
  submission port added with a `POSTFIXMASTER_` variable is checked too;
- rsyslogd is running, otherwise mail is relayed without a trace;
- OpenDKIM, PostSRSd and saslauthd are running when the environment asks for
  them.

Listening sockets are read from the kernel rather than connected to, so the
check leaves nothing in the log.

A daemon that fails to start at all stops the container instead of relaying
mail without the signing or rewriting that was configured, and a daemon that
later gives up on its own exits the container non-zero, so `restart:
on-failure` brings it back.

<p align="right">(<a href="#top">back to top</a>)</p>

<!-- TROUBLESHOOTING -->
## Troubleshooting

### I see `key data is not secure: /etc/opendkim/keys can be read or written by other users` error messages.

Some Docker distributions like Docker for Windows and RancherOS seems to handle
volume permission in way that does not work with OpenDKIM default behavior of
ensuring safe permissions on private keys.

A workaround is to disable the check using a `OPENDKIM_RequireSafeKeys=no` environment variable.

### I set `user:` in my compose file and the container fails with `/bin/bash: /root/run: Permission denied`.

The container has to start as root. `/root` is `0700` and the entrypoint lives
there, so another user cannot even read the script — the message reads like a
broken file mode, but it is the intended one. A readable entrypoint would not
get much further: postfix refuses to run as anyone else, with `the postfix
command is reserved for the superuser`. See
[What runs as root, and what to take away](#what-runs-as-root-and-what-to-take-away)
for what the root processes do, and for the capabilities to drop instead.

Setting `user:` to avoid ownership surprises on mounted directories does not
help either: whatever `user:` says, the container hands the DKIM keys to
`opendkim` and the queue to `postfix` on every start, because that is what
those daemons require. Mount the directories and let the container set them
up; on the host they will show the uids those daemons have inside it.

### Mail is piling up and I want to know what the queue is doing.

Two tools, answering different questions. `qshape` says *which destination owns
the queue, and since when*: destinations down the side, message age in minutes
across the top, doubling each column.

```
docker exec <container> qshape          # incoming and active
docker exec <container> qshape deferred # what has already failed once
```

Weight in the young columns on the left is a problem happening now; everything
in `1280+` is old mail nobody is retrying hard. That ranking is worth most when
mail leaves by `transport_maps` to several places -- with a single
`POSTFIX_relayhost` every message shares one next hop, so the domain axis
describes your senders rather than the fault.

`postqueue -j` says *why*, which `qshape` never sees: one JSON object per queue
file, carrying `delay_reason` and `bounce_reason` per recipient.

```
docker exec <container> postqueue -j | jq -r '.recipients[].delay_reason' | sort | uniq -c
```

<p align="right">(<a href="#top">back to top</a>)</p>

<!-- TESTING -->
## Testing

This project uses [testcontainers](https://testcontainers.com/) with [pytest](https://docs.pytest.org/) for integration testing.

[Mailpit](https://mailpit.axllent.org/) is also used to simulate a remote SMTP server.

```bash
# Create and enable python virtual environment
python -m venv venv
source venv/bin/activate
# Install dependencies
pip install -r tests/requirements.txt
# Start tests
pytest
# Or a single file
pytest tests/test_dkim.py
# Exit python virtual environment
deactivate
```

The tests build the image and run it, so what they check is the image
itself: mail is sent to a container and what comes out of it is read back
from mailpit. `tests/fixtures` has the containers and `tests/helpers.py`
the few things tests keep doing, like waiting for a mail or reading back a
postfix setting.

| File | What it covers |
| --- | --- |
| `test_image.py` | The published image before anything runs: the defaults from the Dockerfile, the declared volumes, the exposed port, the health check, the programs the README has users run out of it |
| `test_defaults.py` | What a relay that is told nothing but where to send does, and the daemons it does not start |
| `test_smtp.py` | The SMTP conversation itself, and that the message handed over is the message that was given |
| `test_sendmail.py` | Whole messages, with their parts, their attachments and their envelope |
| `test_config.py` | `POSTFIX_`, `POSTFIXMASTER_` and `POSTMAP_` variables, from the variable to what postfix does |
| `test_dkim.py` | Signing: the keys, the records to publish, and signatures that verify |
| `test_srs.py` | Envelope sender rewriting, and reversing it again |
| `test_sasl.py` | Clients authenticating to the relay, and the relay authenticating to its next hop |
| `test_client_tls.py` | Encrypting client connections, as the "Securing the relay" section documents it |
| `test_postmaster.py` | Where postfix's reports about itself go, up to the notice arriving |
| `test_logging.py` | What the container logs, and where the `RSYSLOG_` variables send it |
| `test_healthcheck.py` | The health check, against relays with a daemon taken away |
| `test_lifecycle.py` | Starting, restarting, stopping, and the mail that is in the queue meanwhile |
| `test_capabilities.py` | Relaying with everything docker grants by default taken away but the documented set |

Use the `postfix` fixture for a relay with the default configuration,
`postfix_shared` for a configuration several tests read the same way, and
`postfix_factory` when a test changes the container it is given:

```python
def test_signing(postfix_shared, mailpit):
    relay = postfix_shared(env={'OPENDKIM_DOMAINS': 'example.com'})

    send(relay, sender='sender@example.com', subject='signed')

    assert 'dkim-signature' in mailpit.wait_for_message('signed')['headers']
```

Both take `env`, `files` for the configuration that is mounted rather than
set through the environment, and `ports`; `postfix_factory` also takes
`volumes` for the state the image keeps across containers and `kwargs` for
what has to be said to docker itself. Starting a
container is most of what the suite costs: `postfix_shared` starts one per
configuration and keeps it for the whole run, so it is the one to reach for
unless the test kills a daemon, edits a file or restarts the container.

A defect that is understood but not fixed yet is covered by a test that
asserts the behaviour there should be, marked `xfail(strict=True)` with the
issue it belongs to. The run stays green while the defect
is open, and the day it is fixed the strict marker turns the now passing
test red until the marker is removed, so the coverage is never quietly
lost.

When a test fails, the log of the containers it used is part of the pytest
output.

<p align="right">(<a href="#top">back to top</a>)</p>

<!-- LICENSE -->
## License
postfix-relay is licensed under the MIT license. See [LICENSE](LICENSE) for the
full license text.

<p align="right">(<a href="#top">back to top</a>)</p>

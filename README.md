# postfix-relay
Postfix SMTP relay docker image. Useful for sending email without using an
external SMTP server.

Default configuration is an open relay that relies on docker networking for
protection. So be careful to not expose it publicly.

## Usage
`docker pull mwader/postfix-relay` or clone/build it yourself. Docker hub image is built for `amd64`, `arm/v7` and `arm64`.

### Postfix variables

Postfix [configuration options](http://www.postfix.org/postconf.5.html) can be set
using `POSTFIX_<name>` environment variables. See [Dockerfile](Dockerfile) for default
configuration. You probably want to set `POSTFIX_myhostname` (the FQDN used by 220/HELO).

Note that `POSTFIX_myhostname` will change the postfix option
[myhostname](http://www.postfix.org/postconf.5.html#myhostname).

You can modify master.cf using postconf with `POSTFIXMASTER_` variables. All double `__` symbols will be replaced with `/`. For example

### Postfix master.cf variables

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

### Relay Client Authentication
The container includes [Postfix SASL](https://www.postfix.org/SASL_README.html) authentication options that are disabled by default.

#### Example Basic Client PAM Auth
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

### OpenDKIM variables

OpenDKIM [configuration options](http://opendkim.org/opendkim.conf.5.html) can be set
using `OPENDKIM_<name>` environment variables. See [Dockerfile](Dockerfile) for default
configuration. For example `OPENDKIM_Canonicalization=relaxed/simple`.

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

### Logging
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

#### Advanced logging configuration

If configuration via environment variables is not flexible enough it's possible to configure rsyslog directly: `.conf` files in the `/etc/rsyslog.d` directory will be [sorted alphabetically](https://www.rsyslog.com/doc/v8-stable/rainerscript/include.html#file) and included into the primary configuration.

### Timezone
Wrong timestamps in log can be fixed by setting proper timezone.
This parameter is handled by Debian base image.

```
environment:
  ...
  - TZ=Europe/Prague
```

### Known issues

#### I see `key data is not secure: /etc/opendkim/keys can be read or written by other users` error messages.

Some Docker distributions like Docker for Windows and RancherOS seems to handle
volume permission in way that does not work with OpenDKIM default behavior of
ensuring safe permissions on private keys.

A workaround is to disable the check using a `OPENDKIM_RequireSafeKeys=no` environment variable.

## SPF
When sending email using your own SMTP server it is probably a good idea
to setup [SPF](https://en.wikipedia.org/wiki/Sender_Policy_Framework) for the
domain you're sending from.

## DKIM
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
# Exit python virtual environment
deactivate
```

## License
postfix-relay is licensed under the MIT license. See [LICENSE](LICENSE) for the
full license text.

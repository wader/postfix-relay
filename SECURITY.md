# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: the **Security** tab of this
repository, then **Report a vulnerability**. That opens a report only the
maintainers can read, and a private fork to fix it in.

Please do not open a public issue for something that is not already public.
If the button is not there, say in [#115](../../issues/115) that you have a
report and need somewhere to send it — without the details.

There is no bounty, and no committed response time: this is a small project
run in spare time.

## What this image is, before you decide whether you have found a bug

The default configuration is an **open relay** —
`POSTFIX_mynetworks=0.0.0.0/0` and `POSTFIX_smtpd_tls_security_level=none` —
that relies on docker networking to keep strangers off it. That is the
documented product decision, not an oversight: the image exists so other
containers on the same network can send mail without each of them carrying
SMTP credentials, and
[Securing the relay](README.md#securing-the-relay) is the answer for anyone
whose deployment needs more than that.

So these are **not** vulnerabilities in this image, and reports of them will be
closed with a pointer back here:

- that it relays for any client that can reach port 25;
- that it offers no TLS to connecting clients by default;
- that the container starts as root — postfix refuses to run as anyone else,
  and everything that handles mail drops privileges by itself. See
  [What runs as root](README.md#what-runs-as-root-and-what-to-take-away),
  which also lists the capabilities a deployment can drop.

Nor is a scanner report on its own. The published image carries Debian packages
with open CVEs that Debian has assessed as not warranting a stable update;
nobody here can close those. A daily job already scans the published image for
findings that *do* have a fix available, so if `trivy` or `grype` shows you
something, check first whether it has a fixed version — if it does and the
image lacks it, that is worth telling us and probably already has an issue.

## What is in scope

Anything where this repository's own code or configuration does something a
careful reader would not expect from the documentation:

- `run` and `healthcheck` — the entrypoint builds every config file from
  environment variables, writes secrets to disk, and sets the modes and
  ownership on them;
- the defaults in the `Dockerfile`, and what the image ships;
- credential handling: the `_FILE` secret mechanism, the generated
  `sasl_passwd` tables, the SRS secret, the DKIM private keys;
- the workflows, if one of them could be made to leak a secret or publish
  something it should not.

A concrete example of the kind of thing that is in scope: the saslauthd mux
directory is created `root:sasl 0710` precisely because saslauthd makes its own
socket world-writable, and getting that wrong left an unrate-limited password
oracle reachable by any uid in the container. That was a real bug, and it is
the shape of report this file is for.

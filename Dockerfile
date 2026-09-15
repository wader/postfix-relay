FROM debian:trixie-20260824-slim
LABEL org.opencontainers.image.authors="Mattias Wadman <mattias.wadman@gmail.com>"

# postsrsd is optional and only installed where Debian builds it: it is missing
# for armhf in trixie, which is the linux/arm/v7 image.
# perl is spelled out for qshape, which ships in the postfix package but is a
# perl script needing File::Find, and which postfix names in no dependency of
# its own. It is in the image today only because opendkim-tools depends on
# perl:any, which perl-base cannot satisfy -- none of opendkim's own scripts
# needs more than perl-base, so that dependency could correctly be narrowed and
# take qshape down with it on a routine base bump. Naming it here costs nothing
# now and keeps that from happening.
# openssl is spelled out for the same reason: "run" uses it to refuse a DKIM
# key it cannot read, and it is in the image today only because ca-certificates
# depends on it.
#
# full-upgrade is what makes a no-cache rebuild (scan.yml's remediation lever,
# invariant 34) actually re-resolve every package rather than only the ones
# named below. "apt-get install <names>" upgrades a named package and pulls in
# whatever new dependencies it needs, but it never touches a package the base
# image already has installed that is not named here and that nothing new
# depends on a newer version of -- gzip, libpcre2-8-0 and libsqlite3-0 are
# exactly that today: preinstalled in debian:trixie-<date>-slim, named nowhere
# in this file, and left at whatever version the base image shipped even
# against a freshly updated index, however many times the install below runs.
# Measured directly: rerunning this file's install against the pinned base
# with a fresh "apt-get update" left all three at their base-image version;
# adding "apt-get -y upgrade" on top was what actually took them to the
# archive's current one. full-upgrade goes further, the way a fix needing a
# package split or a new dependency would need it to; apt-get and not apt,
# for the stable, scriptable interface the rest of this file relies on
# throughout -- apt's own manual page says as much and asks scripts to use
# apt-get and apt-cache instead.
# autoremove --purge is what full-upgrade can leave behind: a package it
# replaces outright rather than upgrading in place becomes unneeded and this
# is what clears it, config files included, before the image ships.
RUN \
  apt-get update && \
  apt-get -y full-upgrade && \
  apt-get -y --no-install-recommends install \
    procps \
    postfix \
    perl \
    openssl \
    libsasl2-modules \
    libpam-pwdfile \
    sasl2-bin \
    whois \
    opendkim \
    opendkim-tools \
    ca-certificates \
    rsyslog && \
  if apt-cache show postsrsd > /dev/null 2>&1 ; then \
    apt-get -y --no-install-recommends install postsrsd ; \
  fi && \
  apt-get -y autoremove --purge && \
  apt-get clean && \
  rm -rf /var/lib/apt/lists/* \
    /etc/rsyslog.conf \
    /etc/postsrsd.secret
# Default config:
# Open relay, trust docker links for firewalling.
# Try to use TLS when sending to other smtp servers.
# No TLS for connecting clients, trust docker network to be safe
# IPv4 only, because the packaged default is whatever the build machine had.
# The trust store ca-certificates installs is named as the CA file, or postfix
# has nothing to check a server certificate against and the two security levels
# that authenticate the next hop, "verify" and "secure", cannot be used at all.
# CAfile rather than CApath: the smtp client runs chrooted in the queue and
# opens this while it is still root, before the chroot, which a directory of
# hashed links would not survive.
ENV \
  POSTFIX_myhostname=hostname \
  POSTFIX_mydestination=localhost \
  POSTFIX_mynetworks=0.0.0.0/0 \
  POSTFIX_inet_protocols=ipv4 \
  POSTFIX_smtp_tls_security_level=may \
  POSTFIX_smtp_tls_CAfile=/etc/ssl/certs/ca-certificates.crt \
  POSTFIX_smtpd_tls_security_level=none \
  OPENDKIM_Socket=inet:12301@localhost \
  OPENDKIM_Mode=sv \
  OPENDKIM_UMask=002 \
  OPENDKIM_Syslog=yes \
  OPENDKIM_TrustAnchorFile=/usr/share/dns/root.key \
  OPENDKIM_InternalHosts="0.0.0.0/0, ::/0" \
  OPENDKIM_KeyTable=/etc/opendkim/KeyTable \
  OPENDKIM_SigningTable=refile:/etc/opendkim/SigningTable \
  RSYSLOG_TIMESTAMP=no \
  RSYSLOG_LOG_TO_FILE=no \
  SASL_Passwds=""
RUN mkdir -p /etc/opendkim/keys
COPY run healthcheck /root/
VOLUME ["/var/spool/postfix", "/etc/opendkim/keys"]
EXPOSE 25
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["/root/healthcheck"]
CMD ["/root/run"]

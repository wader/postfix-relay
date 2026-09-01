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
RUN \
  apt-get update && \
  apt-get -y --no-install-recommends install \
    procps \
    postfix \
    perl \
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
  apt-get clean && \
  rm -rf /var/lib/apt/lists/* \
    /etc/rsyslog.conf \
    /etc/postsrsd.secret
# Default config:
# Open relay, trust docker links for firewalling.
# Try to use TLS when sending to other smtp servers.
# No TLS for connecting clients, trust docker network to be safe
# IPv4 only, because the packaged default is whatever the build machine had.
ENV \
  POSTFIX_myhostname=hostname \
  POSTFIX_mydestination=localhost \
  POSTFIX_mynetworks=0.0.0.0/0 \
  POSTFIX_inet_protocols=ipv4 \
  POSTFIX_smtp_tls_security_level=may \
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

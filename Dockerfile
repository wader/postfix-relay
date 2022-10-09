# bump: debian-buster-slim /FROM debian:(.*)/ docker:debian|/^buster-.*-slim/|sort
FROM debian:buster-20221004-slim
MAINTAINER Ronan-Yann Lorin rylorin@gmail.com

RUN set -eux; \
  apt-get update && \
  apt-get -y --no-install-recommends install \
    procps \
    postfix \
    libsasl2-modules \
    opendkim \
    opendkim-tools \
    ca-certificates \
    rsyslog && \
  apt-get clean && \
  rm -rf /var/lib/apt/lists/* \
    /etc/rsyslog.conf
# Default config:
# Open relay, trust docker links for firewalling.
# Try to use TLS when sending to other smtp servers.
# No TLS for connecting clients, trust docker network to be safe

ENV \
  POSTFIX_mydestination=localhost \
  POSTFIX_mynetworks=0.0.0.0/0 \
  POSTFIX_smtp_tls_security_level=may \
  POSTFIX_smtpd_tls_security_level=may \
  POSTFIX_virtual_alias_domains= \
  POSTFIX_virtual_alias_maps="hash:/etc/postfix/conf.d/virtual" \
  POSTFIX_smtpd_recipient_restrictions="reject_unauth_destination, permit_sasl_authenticated, permit_mynetworks, check_relay_domains"
  OPENDKIM_Socket=inet:12301@localhost \
  OPENDKIM_Mode=sv \
  OPENDKIM_UMask=002 \
  OPENDKIM_Syslog=yes \
  OPENDKIM_InternalHosts="0.0.0.0/0, ::/0" \
  OPENDKIM_KeyTable=refile:/etc/opendkim/KeyTable \
  OPENDKIM_SigningTable=refile:/etc/opendkim/SigningTable \
  RSYSLOG_TIMESTAMP=no \
  RSYSLOG_LOG_TO_FILE=no
RUN mkdir -p /etc/opendkim/keys
COPY run /root/

VOLUME ["/var/lib/postfix", "/var/mail", "/var/spool/postfix", "/etc/opendkim/keys", "/etc/postfix/conf.d"]

EXPOSE 25

CMD ["/root/run"]

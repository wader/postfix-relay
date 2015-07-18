FROM debian:jessie
MAINTAINER Mattias Wadman mattias.wadman@gmail.com
RUN DEBIAN_FRONTEND=noninteractive \
  apt-get update && \
  apt-get -y install \
    postfix \
    rsyslog
# Default config:
# Open relay, trust docker links for firewalling.
# Try to use TLS when sending to other smtp servers.
# No TLS for connecting clients, trust docker network to be safe
ENV \
  POSTFIX_myhostname=hostname \
  POSTFIX_mydestination=localhost \
  POSTFIX_mynetworks=0.0.0.0/0 \
  POSTFIX_smtp_tls_security_level=may \
  POSTFIX_smtpd_tls_security_level=none
COPY rsyslog.conf /etc/rsyslog.conf
COPY run /root/
VOLUME ["/var/lib/postfix", "/var/mail", "/var/spool/postfix"]
EXPOSE 25
CMD ["/root/run"]

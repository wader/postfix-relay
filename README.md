# postfix-relay
Postfix SMTP relay docker image. Useful for sending email without using an
external SMTP server.

Default configuration is an open relay that rely on docker networking for protection.
So be careful to not expose it publicly.

## Usage
`docker pull mwader/postfix-relay` or clone/build it yourself. 

All postfix configuration options can be set using `POSTFIX_<name>` environment
variables. See [Dockerfile](Dockerfile) for default configuration. You probably
want to set `POSTFIX_myhostname` (the FQDN used by 220/HELO).

#### Using docker run
```
docker run -e POSTFIX_myhostname=smtp.domain.tld mwader/postfix-relay
```

#### Using docker-compose
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

## SPF
When sending email using your own SMTP server it is probably a good idea
to setup [SPF](https://en.wikipedia.org/wiki/Sender_Policy_Framework) for the
domain your sending from.

## DKIM
To enable [DKIM](https://en.wikipedia.org/wiki/DomainKeys_Identified_Mail)
specify a white space separated list of domains in the environment variable
`OPENDKIM_DOMAINS`. At container start key pairs for each domain will be
generated if not found in `/etc/opendkim/keys/<domain>`. To persist keys make
sure to mount a volume for `/etc/opendkim/keys`. If your using
docker-compose it will automatically take care of moving data volumes between
container recreates.

DNS records to configure can be found in the container log or by running
`docker exec <container> cat /etc/opendkim/keys/*/*.txt`.

Default selector is "mail" but can be changed using the syntax `domain.tld=abc`.

## License
postfix-relay is licensed under the MIT license. See [LICENSE](LICENSE) for the
full license text.

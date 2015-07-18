# postfix-relay
Postfix SMTP relay docker image. Useful when wanting to sending email without using an external SMTP server.

## Usage
`docker pull mwader/postfix-relay` or clone and build it yourself. Default postfix is configured to be an open SMTP relay that rely on docker links for firewalling so be careful to not expose it publicly.

All postfix configuration options can be set using POSTFIX_* environment variables. See [Dockerfile](Dockerfile) for default configuration. It will work without making any changes but you might want to change `POSTFIX_myhostname`, the FQDN used by 220/HELO.

#### Using docker run 
```
docker run \
	-e POSTFIX_myhostname=smtp.domain.tld \
	--name smtp \
	mwader/postfix-relay
```
Now run some other container with a link to `smtp` and use it as SMTP server.

#### Using docker-compose

```
app:
  links:
    - smtp
  # some app container config where host "smtp" is used as SMTP server

smtp:
  image: mwader/postfix-relay
  restart: always
  environment:
    - POSTFIX_myhostname=smtp.domain.tld
```

## Tips

When sending using your own SMTP server it is probably a very good idea to setup [SPF](https://en.wikipedia.org/wiki/Sender_Policy_Framework) for the domain your sending mail from.
# postfix-relay
Postfix SMTP relay docker image. Useful for sending email without using an external SMTP server, or for forward emails for virtual domains.

## Usage
`docker pull rylorin/postfix-relay` or clone and build it yourself. Default postfix is configured not to be an open relay as it has to be exposed publicly on Internet to receive your virtual domains emails.

All postfix configuration options can be set using `POSTFIX_<name>` environment variables. See [Dockerfile](Dockerfile) for default configuration. You can use `POSTFIX_myhostname` to set the FQDN used by 220/HELO.

#### Using docker run
```
docker run \
	-e POSTFIX_myhostname=smtp.domain.tld \
	--name smtp \
	rylorin/postfix-relay
```

#### Using docker-compose
```
app:
  # use hostname "smtp" as SMTP server

smtp:
  image: rylorin/postfix-relay
  restart: always
  environment:
    - POSTFIX_myhostname=smtp.domain.tld
```

## SPF
When sending email using your own SMTP server it is probably a very good idea
to setup [SPF](https://en.wikipedia.org/wiki/Sender_Policy_Framework) for the
domain your sending from.

## DKIM
To enable [DKIM](https://en.wikipedia.org/wiki/DomainKeys_Identified_Mail)
specifying a white space separated list of domains in the environment variable
`OPENDKIM_DOMAINS`. At container start key pairs for each domain will be
generated if not found in `/etc/opendkim/keys/<domain>`. To persist the keys make
sure to add a host directory volume for `/etc/opendkim/keys`. If your using
docker-compose it will automatically take care of moving data volumes between
container recreates.

DNS records to configure can be found in the container log or by running
`docker exec <container> cat /etc/opendkim/keys/*/mail.txt`.

## Virtual domains forwarding
You can set up Postfix to forward all mails coming on your domain email to
your other address like gmail.
```
mail for superman@site.com ==> [ site.com Postfix server ] ==> forwarded to superman@gmail.com
```

### DNS settings of domain
First it's necessary to ensure that all mail for your domain will be delivered to your Postfix server.
You do that by adding a MX record to your DNS, like:
```
site.com. 85100 IN MX 10 mail.site.com.
```
where `mail.site.com.` is your Postfix server address.

### Docker host setting
On your docker host you need to setup a directory which will contains your `virtual` address mapping file.
This directory will be mounted to the docket image and shared with Postfix.
Your virtual file should look like:
```
@site.com			superman@gmail.com superman@yahoo.com
contact@site2.com	superman2@gmail.com
@site3.com			superman3@gmail.com
```
Read Postfix documentation for more information.

### Docker image setup
Assemble everything using docker-compose:
```
version: '2'
services:
  app:
    # use hostname "smtp" as SMTP server
  smtp:
    image: rylorin/postfix-relay
    environment:
      POSTFIX_virtual_alias_domains: site.com site2.com site3.com
      POSTFIX_myhostname: mail.site.com
    volumes:
    - /path/to/your/host/conf.d:/etc/postfix/conf.d:rw
    ports:
    - 25:25/tcp
```

## License
postfix-relay is licensed under the MIT license. See [LICENSE](LICENSE) for the
full license text.

# Not built, and no part of any image. This file exists so that the version of
# the released image the upgrade tests start from can be seen by a bot that
# only understands Dockerfiles, the same reason tests/mailpit.Dockerfile is
# here: Dependabot's docker ecosystem matches any file whose name contains
# "dockerfile", and .github/dependabot.yml already points one at this
# directory, which lists every file it matches rather than a single name.
#
# tests/fixtures/postfix.py reads the tag back out of the line below, so this
# is the one place the version is written.
#
# A release and not "latest", which is the other image this could name.
# "latest" is rebuilt from master on every merge, so on master it is the very
# image under test and an upgrade to itself proves nothing; it also moves
# without anything in the tree recording that it did, which is what makes a
# failure impossible to attribute. A release stays put until this line is
# changed, and it is what people are actually upgrading from.
#
# tests/ is excluded by .dockerignore, so this never reaches the build context
# of the image itself.
FROM mwader/postfix-relay:1.2.17

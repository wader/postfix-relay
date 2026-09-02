# Not built, and no part of any image. This file exists so that the version of
# the mailpit image can be seen by a bot that only understands Dockerfiles:
# Dependabot's docker ecosystem matches any file whose name contains
# "dockerfile", and .github/dependabot.yml points one at this directory.
#
# tests/fixtures/mailpit.py reads the tag back out of the line below, so this
# is the one place the version is written and there is nothing to keep in step
# by hand. Changing it here changes what the suite tests against.
#
# tests/ is excluded by .dockerignore, so this never reaches the build context
# of the image itself.
FROM axllent/mailpit:v1.31.0

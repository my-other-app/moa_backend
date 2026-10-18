# syntax=docker/dockerfile:1
# BuildKit syntax, for the pip cache mounts below. docker-compose v2 builds
# with BuildKit already; the deploy sets DOCKER_BUILDKIT=1 and
# COMPOSE_DOCKER_CLI_BUILD=1 for v1, and checks docker buildx is there first.
FROM python:3.11-bookworm

WORKDIR /backend

# Install wkhtmltopdf and OpenSSL (for wallet signing)
RUN apt-get update -y && apt-get install -y \
    wkhtmltopdf \
    openssl \
    && rm -rf /var/lib/apt/lists/*

# postgresql-client, for the on-demand database snapshot endpoint.
#
# Pinned to 17 from PGDG rather than Debian's own package: bookworm ships
# client 15, and pg_dump refuses outright to dump a server newer than itself
# -- the database here is 17.4, so the bundled client would fail every time
# with a version error rather than producing anything.
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*

# Python packages, in two layers, so that a build is cheap for the server.
#
# On 2026-10-18 a one-line change to requirements.txt reinstalled every
# package: 5 minutes collecting, 16 installing, 2 exporting, while the live
# API on the same box timed out. Now:
# - requirements-base.txt (numpy, pandas, boto3, firebase-admin, reportlab...)
#   is its own layer, reused until that file changes; a change to
#   requirements.txt installs only what that file adds (35 packages, not 99).
# - pip's downloads stay in a BuildKit cache mount between builds, so a
#   rebuilt layer does not download again. The cache is a mount, not part of
#   any layer, so the image is no bigger; --no-cache-dir is dropped only here,
#   where the cache lives in the mount.
COPY ./requirements-base.txt /backend/requirements-base.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --disable-pip-version-check -r /backend/requirements-base.txt

COPY ./requirements.txt /backend/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --disable-pip-version-check -r /backend/requirements.txt

COPY ./ /backend/

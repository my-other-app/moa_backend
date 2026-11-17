# syntax=docker/dockerfile:1
# BuildKit syntax, for the pip cache mounts below. docker-compose v2 builds
# with BuildKit already; the deploy sets DOCKER_BUILDKIT=1 and
# COMPOSE_DOCKER_CLI_BUILD=1 for v1, and checks docker buildx is there first.
# Pinned by digest (python 3.11.18 on bookworm, 16 Nov 2026), so a build
# never moves the base unannounced; moving it is an edit here, and then the
# first build reinstalls every layer.
FROM python:3.11-bookworm@sha256:726d304906fa5eb468e5531e366548d1903286352f5adb54ef508ac01fbd80c6

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

# The code compiled here, once: the user below cannot write __pycache__
# beside it, and every process then starts without compiling.
RUN python -m compileall -q -j 0 /backend/app /backend/migrations /backend/doth.py /backend/main.py

# Its own unprivileged user, uid 10001, the same on every build, so the
# host's folders it writes (/var/backups/myotherapp) and reads
# (/srv/moa/secrets) can be given to it once (ops/server/README.md, "The API
# as a non-root user"). The home is for the font cache wkhtmltopdf keeps;
# everything else it writes goes to /tmp or the backups folder. logs/ is
# where APP_SQL_LOG=true writes. ops/server/compose.yml keeps root until the
# owner has made that change (MOA_BACKEND_USER).
RUN groupadd --system --gid 10001 moa \
    && useradd --system --no-log-init --uid 10001 --gid 10001 --home-dir /home/moa --create-home \
       --shell /usr/sbin/nologin moa \
    && install -d -o 10001 -g 10001 /backend/logs
USER 10001:10001

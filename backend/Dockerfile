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

COPY ./requirements.txt /backend/requirements.txt
RUN pip install --no-cache-dir -r /backend/requirements.txt

COPY ./ /backend/

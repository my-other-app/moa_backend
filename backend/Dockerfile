FROM python:3.11-bookworm

WORKDIR /backend

# Install wkhtmltopdf and OpenSSL (for wallet signing)
RUN apt-get update -y && apt-get install -y \
    wkhtmltopdf \
    openssl \
    && rm -rf /var/lib/apt/lists/*

COPY ./requirements.txt /backend/requirements.txt
RUN pip install --no-cache-dir -r /backend/requirements.txt

COPY ./ /backend/

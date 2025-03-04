FROM python:3.11-slim

WORKDIR /backend

COPY ./ /backend/

RUN pip install -r /backend/requirements.txt
RUN apt-get update -y && apt-get install wkhtmltopdf -y

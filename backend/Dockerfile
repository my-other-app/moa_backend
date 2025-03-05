FROM python:3.11-slim

WORKDIR /backend

RUN apt-get update -y && apt-get install -y wkhtmltopdf

COPY ./requirements.txt /backend/requirements.txt
RUN pip install -r /backend/requirements.txt

COPY ./ /backend/

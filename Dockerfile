ARG PYTHON_VERSION=3.12
FROM python:$PYTHON_VERSION-slim as builder
# LABEL maintainer="Xavier Petit <nuxion@gmail.com>"
MAINTAINER Xavier Petit <nuxion@gmail.com>
USER root
SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND noninteractive
ENV PATH $PATH:/root/.local/bin

COPY requirements.txt /tmp/requirements.txt
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends curl make git vim build-essential \
    && pip install --upgrade pip \
    && pip install --user -r /tmp/requirements.txt
# COPY . /opt/code
# WORKDIR /opt/code
# RUN pip install --user -r requirements.jupyter.txt .

FROM python:3.12-slim as app
MAINTAINER Xavier Petit <nuxion@gmail.com>
ENV DEBIAN_FRONTEND=noninteractive
# VOLUME /workspace
RUN apt-get update -y \
    # && apt-get install -y --no-install-recommends git vim wget unzip \
    && groupadd app -g 1000 \
    && useradd -m -d /home/app app -u 1000 -g 1000 \
    && mkdir /data \
    && chown app:app /data \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=builder --chown=app:app /root/.local /home/app/.local/
COPY --chown=app:app . /app

USER app
WORKDIR /app
ENV BASE_PATH /app
ENV DATA_DIR /app/data
ENV PATH=$PATH:/home/app/.local/bin
ENV PYTHONPATH /app
CMD ["bash", "-c", "srv web --host 0.0.0.0 -L"]

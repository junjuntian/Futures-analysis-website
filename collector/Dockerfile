FROM python:3.13.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    COLLECTOR_CREDENTIALS_FILE=/run/secrets/collector-credentials

RUN groupadd --system --gid 10001 collector \
    && useradd --system --uid 10001 --gid collector --home /nonexistent --shell /usr/sbin/nologin collector

WORKDIR /app
COPY collector/requirements.lock /app/requirements.lock
RUN python -m pip install --requirement /app/requirements.lock

COPY collector/src /app/src
ENV PYTHONPATH=/app/src
ENTRYPOINT ["python", "-m", "futures_collector"]

FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libmariadb-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
COPY scripts ./scripts
COPY init.sql ./init.sql

RUN pip install --upgrade pip setuptools wheel \
    && pip install .[dev]

FROM python:3.11-slim-bookworm AS runtime

ARG COPILOT_CLI_VERSION=latest

ENV PATH="/opt/venv/bin:${PATH}" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        libmariadb3 \
        tini \
    && curl -fsSL https://gh.io/copilot-install | VERSION="${COPILOT_CLI_VERSION}" PREFIX=/usr/local bash \
    && command -v copilot >/dev/null 2>&1 \
    && copilot help >/dev/null \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system marunage \
    && useradd --system --gid marunage --create-home --home-dir /home/marunage marunage

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=marunage:marunage pyproject.toml README.md ./
COPY --chown=marunage:marunage src ./src
COPY --chown=marunage:marunage tests ./tests
COPY --chown=marunage:marunage scripts ./scripts
COPY --chown=marunage:marunage init.sql ./init.sql
COPY --chown=marunage:marunage Dockerfile ./Dockerfile
COPY --chown=marunage:marunage docker-compose.prod.yml ./docker-compose.prod.yml
COPY --chown=marunage:marunage docker-compose.test.yml ./docker-compose.test.yml
RUN chmod 755 /app/scripts/entrypoint.sh

USER marunage

ENTRYPOINT ["/usr/bin/tini", "--", "/app/scripts/entrypoint.sh"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python", "scripts/healthcheck.py"]
CMD ["python", "scripts/service_runner.py", "brain"]
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.215
ARG PLAYWRIGHT_MCP_VERSION=0.0.78

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TIAAA_HOME=/data \
    TIAAA_FORCE_HEADLESS=1 \
    TIAAA_CHROME_NO_SANDBOX=1 \
    TIAAA_PLAYWRIGHT_MCP_COMMAND=playwright-mcp \
    CHROME_PATH=/usr/bin/chromium \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        ca-certificates \
        fonts-liberation \
        fonts-noto-color-emoji \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}"

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src

RUN python3 -m venv /opt/venv \
    && pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin tiaaa \
    && mkdir -p /data \
    && chown -R tiaaa:tiaaa /data /app

ENV HOME=/data
USER tiaaa
EXPOSE 8787
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=3)" || exit 1

CMD ["tiaaa", "serve", "--host", "0.0.0.0", "--port", "8787", "--no-open"]

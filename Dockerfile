# grokbot-bridge — shared phone-call bridge MCP for Grok Bot (streamable HTTP)
FROM python:3.12-slim

ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/kitepon/grokbot-bridge" \
      org.opencontainers.image.revision="${VCS_REF}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY directory.json ./directory.json
RUN pip install --no-cache-dir .

EXPOSE 18910

CMD ["grokbot-bridge"]

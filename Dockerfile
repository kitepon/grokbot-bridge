# grokbot-bridge — shared phone-call bridge MCP for Grok Bot (streamable HTTP)
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 18910

CMD ["grokbot-bridge"]

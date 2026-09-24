# grokbot-bridge — shared phone-call bridge MCP for Grok Bot (streamable HTTP)
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY directory.json ./directory.json
RUN pip install --no-cache-dir .

EXPOSE 18910

CMD ["grokbot-bridge"]

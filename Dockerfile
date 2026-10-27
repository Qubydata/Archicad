# The Archicad MCP cloud service (sign-in, Stripe subscriptions, metered Claude access).
#   docker build -t archicad-mcp-cloud .
#   docker run -p 8080:8080 -v archicad-mcp-data:/data --env-file cloud.env archicad-mcp-cloud
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV DATABASE_PATH=/data/cloud.db PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
CMD ["/app/.venv/bin/archicad-mcp-cloud", "serve"]

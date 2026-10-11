# Muse MCP bridge for the Pebble Index 01 smart ring.
# Streamable HTTP on :8765, bearer-token auth.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy project metadata + sources before install (src-layout: pip needs src/ present).
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --upgrade pip && pip install .

RUN useradd --create-home --shell /usr/sbin/nologin muse \
    && chown -R muse:muse /app
USER muse

EXPOSE 8765

# MCP_HOST defaults to 0.0.0.0 in server.py; PORT from env.
CMD ["python", "-m", "pebble_index_mcp.server"]

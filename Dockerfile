# Use Python 3.11 on Debian 12 Bookworm (slim variant for production efficiency)
FROM python:3.11-slim-bookworm

# Prevents Python from writing pyc files; ensures unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install Node.js 20 LTS (runtime for the Node-based MCP servers) + minimal system deps.
# gnupg is only needed to add the NodeSource apt key, so purge it afterwards in the same
# layer to keep the image lean.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    gosu \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for the application. The entrypoint (run as root) fixes ownership of the
# host-mounted data dir, then drops to this user via gosu — so the app + MCP subprocesses
# run unprivileged with no host-side chown required.
RUN useradd --system --create-home --uid 10001 appuser

# Bake the Node-based MCP servers into the image (pinned) so nothing is downloaded from
# npm at runtime — the previous `npx -y` launch fetched these on every cold container.
RUN npm install -g \
    @notionhq/notion-mcp-server@2.4.1 \
    tavily-mcp@0.2.20 \
    && npm cache clean --force

# Install Python dependencies
# Copy only requirements first to leverage Docker layer cache
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir uv && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir mcp-server-sqlite==2025.4.25  # bake the sqlite MCP server (was uvx-fetched at runtime)

# Copy the rest of the application code.
# CACHEBUST forces this layer (and only this layer) to rebuild on demand so code
# changes are always picked up — the dependency layers above stay cached. Pass a
# changing value to guarantee a fresh copy:
#   docker compose build --build-arg CACHEBUST=$(date +%s)
# (Works around a BuildKit COPY-cache staleness seen on the OneDrive-backed source dir.)
ARG CACHEBUST=0
COPY . .

# Ensure the entrypoint is executable and app code is owned by the non-root user.
RUN chmod +x /app/docker-entrypoint.sh && chown -R appuser:appuser /app

# Entrypoint runs as root (fix data-dir perms) then execs the CMD as appuser via gosu.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "main.py"]

# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Create non-root user with UID 1000 (matches typical host user)
RUN groupadd -g 1000 gridbear && \
    useradd -u 1000 -g gridbear -m -s /bin/bash gridbear

WORKDIR /app

# Install system dependencies (including Playwright/Chromium deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    gosu \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js for Claude CLI
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@latest @openai/codex typescript

# Install uv for fast Python package management
RUN pip install uv

# B1: Install PyTorch CPU-only BEFORE sentence-transformers (saves ~1.5GB)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system torch --index-url https://download.pytorch.org/whl/cpu

# Copy dependencies file + version (needed for dynamic version resolution)
COPY pyproject.toml .
COPY core/__version__.py core/__version__.py

# B2+B3: Use uv with cache mount (10-100x faster rebuilds)
# Install all optional groups (plugins need their deps at runtime)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system ".[dev,all]" && \
    uv pip install --system "starlette<1.0.0" && \
    uv pip install --system py-spy

# B5: Pre-download embedding model (~90MB, avoids download at first startup)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Install Playwright and browsers (both Python and npm versions)
# PLAYWRIGHT_BROWSERS_PATH shared between root (build) and gridbear (runtime)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system playwright \
    && playwright install chromium \
    && npm install -g @playwright/mcp \
    && (npx playwright install chrome 2>/dev/null || echo "Chrome not available on this architecture, using Chromium") \
    && npx @playwright/mcp --help || true \
    && chmod -R o+rx /opt/pw-browsers

# Install GitHub MCP Server binary (architecture-aware)
RUN ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "amd64" ]; then GHARCH="x86_64"; else GHARCH="$ARCH"; fi \
    && curl -sL "https://github.com/github/github-mcp-server/releases/download/v0.30.3/github-mcp-server_Linux_${GHARCH}.tar.gz" \
    | tar xz -C /usr/local/bin github-mcp-server \
    && chmod +x /usr/local/bin/github-mcp-server

# Install Google Sheets MCP server + boto3 for S3 backup uploads
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system mcp-google-sheets boto3

COPY . .

# Build Tailwind CSS for admin UI
RUN npm install --save-dev tailwindcss @tailwindcss/forms daisyui \
    && npx tailwindcss -i ./ui/static/css/input.css -o ./ui/static/css/output.css --minify \
    && rm -rf node_modules

# Create directories and set ownership
RUN mkdir -p /app/data/attachments /app/data/models /app/data/avatars /app/credentials /home/gridbear/.claude /home/gridbear/.codex && \
    chown -R gridbear:gridbear /app /home/gridbear

# Entrypoint fixes bind-mount ownership then drops to gridbear user
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "main.py"]

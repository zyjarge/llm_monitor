FROM python:3.11-slim

WORKDIR /app

# Speed up uv + pip with Aliyun PyPI mirror (same network as host ECS)
ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# Install uv
RUN pip install --no-cache-dir uv

# Copy dependency files first (better layer cache)
COPY pyproject.toml ./
RUN uv sync --no-dev

# Install Playwright Chromium
RUN uv run playwright install chromium --with-deps

# Copy source
COPY scraper ./scraper

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["uv", "run", "python", "-m", "scraper.src.main"]

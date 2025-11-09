FROM python:3.13-slim

WORKDIR /app

ENV UV_PROJECT_ENVIRONMENT="/usr/local/"
ENV UV_COMPILE_BYTECODE=1
# Optimize pip/transformers caching
ENV PIP_NO_CACHE_DIR=1
ENV HF_HUB_DISABLE_SYMLINKS_WARNING=1

# Install system dependencies and uv in a single layer
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    pip install --no-cache-dir uv && \
    rm -rf /var/lib/apt/lists/*

# Copy dependency files and install dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy application code
COPY src/ ./src/
COPY main.py .

# Create models directory for sentiment model cache
RUN mkdir -p ./models

EXPOSE 8080
CMD ["python", "main.py"]
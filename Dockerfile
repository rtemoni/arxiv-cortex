FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARXIV_CORTEX_DATA_DIR=/data \
    HF_HOME=/models/huggingface

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 cortex \
    && mkdir -p /data /models/huggingface \
    && chown -R cortex:cortex /data /models

USER cortex
EXPOSE 5000
VOLUME ["/data", "/models"]

CMD ["uv", "run", "--frozen", "arxiv-cortex", "--host", "0.0.0.0", "--port", "5000"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HOME=/tmp/bci-query-engine
ENV XDG_CONFIG_HOME=/tmp/bci-query-engine/.config
ENV XDG_CACHE_HOME=/tmp/bci-query-engine/.cache

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir .

RUN useradd --no-create-home --shell /bin/false appuser
RUN mkdir -p /tmp/bci-query-engine/.config /tmp/bci-query-engine/.cache \
    && chown -R appuser:appuser /tmp/bci-query-engine
USER appuser

EXPOSE 8300

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300"]

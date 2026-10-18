FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONIOENCODING=UTF-8

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ohnaarr/ ./ohnaarr/

# Non-root; /config holds the state db and the optional overrides file.
RUN useradd --create-home --uid 1000 ohnaarr \
    && mkdir -p /config \
    && chown -R ohnaarr:ohnaarr /config /app
USER ohnaarr

VOLUME ["/config"]
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-m", "ohnaarr.healthcheck"]

ENTRYPOINT ["python", "-m", "ohnaarr.main"]
CMD ["run"]

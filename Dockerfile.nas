FROM python:3.12-slim

WORKDIR /AstrBot

ENV PYTHONPATH=/AstrBot:/AstrBot/dc_engines \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        dbus-x11 \
        gnome-keyring \
        libsecret-1-0 \
        nodejs \
        npm \
    && npm install --global @openai/codex@0.144.1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock /AstrBot/

RUN python -m pip install --no-cache-dir uv \
    && uv export --quiet \
        --no-dev \
        --no-emit-project \
        --format requirements.txt \
        --output-file /tmp/requirements.txt \
        --frozen \
    && uv pip install \
        --system \
        --no-cache-dir \
        --requirement /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

COPY . /AstrBot/

RUN chmod +x /AstrBot/deploy/nas-unified/runtime-bin/start-with-keyring.sh

EXPOSE 6185

ENTRYPOINT ["/AstrBot/deploy/nas-unified/runtime-bin/start-with-keyring.sh"]
CMD ["python", "main.py"]

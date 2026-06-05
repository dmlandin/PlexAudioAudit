FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      mkvtoolnix \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pyyaml==6.0.2

WORKDIR /app
COPY audit.py /app/audit.py
COPY remediate.py /app/remediate.py
COPY config.yaml /app/config.yaml

ENTRYPOINT ["python", "/app/audit.py"]
CMD ["--config", "/app/config.yaml"]

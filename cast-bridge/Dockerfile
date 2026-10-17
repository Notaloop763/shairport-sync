FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir PyChromecast==14.0.10
WORKDIR /app
COPY bridge.py /app/bridge.py
ENTRYPOINT ["python", "-u", "/app/bridge.py"]

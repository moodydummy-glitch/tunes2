FROM python:3.11-slim

# No FFmpeg needed — Lavalink handles all audio processing
RUN apt-get update && \
    apt-get install -y libffi-dev python3-dev gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .

CMD ["python", "player_bot.py"]

FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y ffmpeg libffi-dev libnacl-dev python3-dev gcc cargo rustc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .

CMD ["python", "player_bot.py"]

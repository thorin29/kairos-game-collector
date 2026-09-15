FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY game_collector.py .

# The SQLite buffer lives on a mounted volume, not inside the image.
ENV DB_PATH=/data/game_playtime.db
VOLUME ["/data"]

CMD ["python", "-u", "game_collector.py"]

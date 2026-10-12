FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kinobot ./kinobot
ENV DB_PATH=/data/kinobot.db
CMD ["python", "-m", "kinobot", "run"]

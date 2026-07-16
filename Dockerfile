FROM python:3.11-slim

WORKDIR /app

# Force Python to flush stdout/stderr buffer in real-time to show logs instantly on Render
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Explicitly expose Render's default web service port to the load-balancer
EXPOSE 10000

CMD ["python", "bot.py"]
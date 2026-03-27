FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p downloads

EXPOSE 8080

CMD gunicorn app:app --bind 0.0.0.0:8080 --workers 2 --timeout 300

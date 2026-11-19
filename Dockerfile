# Recipe the host (Railway, Render, Fly...) follows to build and run Sift.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    NEWSINTEL_LIVE=1 \
    NEWSINTEL_DB=/data/newsintel.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# One process only: the refresh loop and the database live inside it.
# --proxy-headers lets the app see the public https:// address behind the host's proxy.
CMD uvicorn newsintel.api:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips="*"

FROM node:20-alpine AS frontend-vendor
WORKDIR /build
COPY package.json ./
RUN npm install --omit=dev --ignore-scripts

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=3000 \
    DATA=/data \
    CACHE=/cache \
    MEDIA=/media
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 568 gallery \
    && useradd --uid 568 --gid 568 --no-create-home --shell /usr/sbin/nologin gallery \
    && mkdir -p /app /data /cache \
    && chown -R 568:568 /data /cache
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=568:568 app.py worker.py cli.py ./
COPY --chown=568:568 public ./public
COPY --from=frontend-vendor --chown=568:568 /build/node_modules/photoswipe/dist ./vendor
USER 568:568
EXPOSE 3000
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --access-logfile - --error-logfile - app:app"]

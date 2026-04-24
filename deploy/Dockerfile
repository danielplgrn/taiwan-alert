FROM python:3.12-slim

WORKDIR /opt/taiwan-alert

# Install cron and nginx
RUN apt-get update && \
    apt-get install -y --no-install-recommends cron nginx && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Nginx config — serve static dashboard on port 8080
RUN echo 'server { \
    listen 8080; \
    root /opt/taiwan-alert/web; \
    index index.html; \
    location / { try_files $uri $uri/ =404; } \
}' > /etc/nginx/sites-available/default

# Install crontab
COPY deploy/crontab.txt /etc/cron.d/taiwan-alert
RUN chmod 0644 /etc/cron.d/taiwan-alert && crontab /etc/cron.d/taiwan-alert

# Create data/log dirs
RUN mkdir -p data && touch /var/log/taiwan-alert.log

# Startup: run initial collection, then start cron + nginx
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

CMD ["/entrypoint.sh"]

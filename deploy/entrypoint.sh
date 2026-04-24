#!/bin/bash
set -e

# Pass env vars to cron environment
printenv | grep -E '^(APIFY_|CLOUDFLARE_|TAIWAN_ALERT_SLACK)' >> /etc/environment

# Run initial collection (all groups)
echo "Running initial collection..."
cd /opt/taiwan-alert
python3 collect.py 2>&1 | tee -a /var/log/taiwan-alert.log

# Start cron daemon
echo "Starting cron..."
cron

# Start nginx in foreground
echo "Starting nginx on :8080..."
nginx -g 'daemon off;'

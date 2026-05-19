#!/usr/bin/env bash
set -o errexit
set -o nounset
set -o pipefail

# Start the telegram bot in the background
python manage.py runbot &

# Start the web server in the foreground
gunicorn core.wsgi:application --bind 0.0.0.0:${PORT:-8000}

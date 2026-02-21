#!/bin/sh
set -e
if [ "$FLASK_DEBUG" = "true" ]; then
  exec python -m flask run --host=0.0.0.0 --port=8000 --debug --reload
else
  exec python -m flask run --host=0.0.0.0 --port=8000
fi

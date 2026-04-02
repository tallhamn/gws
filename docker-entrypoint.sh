#!/bin/sh
set -eu

alembic -c /app/alembic.ini upgrade head

exec "$@"

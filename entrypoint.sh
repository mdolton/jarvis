#!/bin/sh
set -e

echo "jarvis: running database migrations..."
alembic -x db_url="sqlite+aiosqlite:///./data/jarvis.db" upgrade head

echo "jarvis: starting service..."
exec python -m jarvis serve "$@"

#!/usr/bin/env bash
set -e
[ -f .env ] || { cp .env.example .env; echo "Created .env — fill KITE_API_KEY/SECRET first"; exit 1; }
pip install -q -r requirements.txt
uvicorn app.main:app --reload --port ${PORT:-8420}

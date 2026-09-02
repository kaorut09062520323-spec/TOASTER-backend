#!/bin/zsh
set -e

cd "$(dirname "$0")"
source ../.venv/bin/activate
python snapshot_daily_ranking.py

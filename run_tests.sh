#!/usr/bin/env bash
# Every test in this repo runs with plain python -- no pytest, no database,
# no network. Run before every deploy.
set -e
cd "$(dirname "$0")"
python tests/test_goose.py
echo
python tests/test_week_engine.py
echo
python tests/test_watch.py
echo
python tests/test_templates.py

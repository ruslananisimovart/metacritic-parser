#!/usr/bin/env sh
# Metacritic parser - service control for Linux and macOS (the same menu as Metacritic_Parser.bat).
#
#   ./metacritic_parser.sh            menu: 1 start, 2 restart, 3 stop, 4 YouTube login, 0 exit
#   ./metacritic_parser.sh start | restart | stop | status
#
# Nothing is installed: no systemd unit, no cron job. The service keeps running after the SSH session ends.
# Port: SERVICE_PORT=8010 ./metacritic_parser.sh start
# Listen on all interfaces (VPS): SERVICE_HOST=0.0.0.0 ./metacritic_parser.sh start

cd "$(dirname "$0")" || exit 1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUTF8=1

# interpreter lookup: project virtual environment, then python3 and python from PATH
PYEXE=""
for candidate in .venv/bin/python venv/bin/python; do
    if [ -x "$candidate" ]; then PYEXE="$candidate"; break; fi
done
if [ -z "$PYEXE" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then PYEXE="$candidate"; break; fi
    done
fi
if [ -z "$PYEXE" ]; then
    echo "Python 3.12 not found. Install it, then run: python3 -m pip install -r requirements.txt"
    exit 1
fi
if ! "$PYEXE" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >/dev/null 2>&1; then
    echo "Python 3.12 or newer is required, found an older one: $PYEXE"
    exit 1
fi

exec "$PYEXE" tools/service_control.py "$@"

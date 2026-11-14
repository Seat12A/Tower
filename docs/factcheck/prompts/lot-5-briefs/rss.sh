#!/bin/bash
# usage: rss.sh <tree> <db> ; prints RSS at 8 s of gate, hangar (5 services), intercom --dry-run
TREE=$1; DB=$2; PY=/home/user/Tower/.venv/bin/python
export PYTHONPATH=$TREE/src TOWER_DATABASE_URL=postgresql://postgres@localhost:5433/$DB TOWER_LLM_FAKE=1 TOWER_LOG_LEVEL=warn
cd $TREE
run() { "$PY" -c "import sys; from tower.cli import main; sys.argv=['tower']+sys.argv[1:]; main()" "$@" >/dev/null 2>&1 & echo $!; }
G=$(TOWER_PORT=8765 run gate); sleep 8; echo "gate $(grep VmRSS /proc/$G/status)"
H=$(TOWER_SERVICES=ping,ingest,radar,factcheck,investigate run hangar); sleep 8; echo "hangar5 $(grep VmRSS /proc/$H/status)"
I=$(TOWER_GATE_URL=http://127.0.0.1:8765 TOWER_GATE_TOKEN=x run intercom --dry-run); sleep 8; echo "intercom $(grep VmRSS /proc/$I/status 2>/dev/null || echo exited)"
kill $G $H $I 2>/dev/null; wait 2>/dev/null

#!/bin/bash
S=$(dirname $0); cd /home/user/Tower
pkill -f 'tower (gate|hangar)' ; pkill -f fake_x.py; sleep 1
dropdb -h localhost -p 5433 -U postgres --if-exists tower_smoke; createdb -h localhost -p 5433 -U postgres tower_smoke
source $S/env.sh
echo '{"users": {"1001": "AccountA", "1002": "AccountB"}, "posts": []}' > $S/posts.json
uv run tower migrate >/dev/null 2>&1; uv run tower llm seed >/dev/null 2>&1
uv run tower create-service-token smoke --scopes dossiers:read dossiers:create dossiers:review dossiers:decide dossiers:publish events:read watchlist:read watchlist:write usage:read factcheck:request claim_groups:manage reference_facts:write reference_facts:validate profiles:read profiles:manage levels:override 2>/dev/null | grep -oE '[A-Za-z0-9_.-]{30,}' | tail -1 > $S/token
for s in "111 admin" "222 editor" "333 reviewer"; do set -- $s; uv run tower staff $1 --roles $2 --name U$1 >/dev/null 2>&1; done
nohup /home/user/Tower/.venv/bin/python $S/../bin/fake_x.py $S/posts.json 8799 > $S/fakex.out 2>&1 &
nohup uv run tower gate > $S/gate.log 2>&1 &
nohup uv run tower hangar > $S/hangar.log 2>&1 &
sleep 8
$S/api.sh POST /v1/watched-accounts '{"platform":"x","handle":"AccountA","category":"minister","priority":10}' 111 >/dev/null
$S/api.sh POST /v1/watched-accounts '{"platform":"x","handle":"AccountB","category":"minister","priority":10}' 111 >/dev/null
echo started

#!/bin/bash
# api.sh METHOD PATH [JSON] [STAFF_ID]
S=$(dirname $0); T=$(cat $S/token)
args=(-s --noproxy '*' -X $1 "http://127.0.0.1:8765$2" -H "Authorization: Bearer $T" -H 'content-type: application/json')
[ -n "$4" ] && args+=(-H "X-Acting-Discord-Id: $4")
[ -n "$3" ] && args+=(-d "$3")
curl "${args[@]}"

"""Minimal fake X API v2 for the smoke test. Reads posts.json on every request:
{"users": {"<id>": "<username>"}, "posts": [{"id","author_id","text","lang","created_at"}]}"""

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DATA = Path(sys.argv[1])
LOG = Path(sys.argv[1]).with_suffix(".log")


def load() -> dict:
    return json.loads(DATA.read_text())


def post_json(p: dict) -> dict:
    return {
        "id": p["id"],
        "text": p["text"],
        "author_id": p["author_id"],
        "lang": p.get("lang", "fr"),
        "created_at": p.get("created_at", "2026-11-11T10:00:00.000Z"),
        "public_metrics": {"like_count": 3, "retweet_count": 1, "reply_count": 0,
                           "quote_count": 0, "impression_count": 120},
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: ANN002
        with LOG.open("a") as f:
            f.write(self.path + "\n")

    def send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        d = load()
        url = urlsplit(self.path)
        path = url.path
        users = d["users"]
        self.log_message()
        if m := re.fullmatch(r"/2/users/by/username/(\w+)", path):
            for uid, name in users.items():
                if name.lower() == m.group(1).lower():
                    return self.send(200, {"data": {"id": uid, "username": name, "name": name,
                                                    "public_metrics": {"followers_count": 1000}}})
            return self.send(404, {"errors": [{"title": "Not Found Error"}]})
        if m := re.fullmatch(r"/2/users/(\d+)/tweets", path):
            uid = m.group(1)
            since = parse_qs(url.query).get("since_id", ["0"])[0]
            posts = [p for p in d["posts"] if p["author_id"] == uid and int(p["id"]) > int(since)]
            posts.sort(key=lambda p: int(p["id"]), reverse=True)
            meta = {"result_count": len(posts)}
            if posts:
                meta["newest_id"] = posts[0]["id"]
                meta["oldest_id"] = posts[-1]["id"]
            inc = {"users": [{"id": uid, "username": users.get(uid, "u")}]}
            return self.send(200, {"data": [post_json(p) for p in posts], "includes": inc,
                                   "meta": meta} if posts else {"meta": meta})
        if m := re.fullmatch(r"/2/users/(\d+)", path):
            uid = m.group(1)
            if uid in users:
                return self.send(200, {"data": {"id": uid, "username": users[uid], "name": users[uid]}})
            return self.send(404, {"errors": [{"title": "Not Found Error"}]})
        if m := re.fullmatch(r"/2/tweets/(\d+)", path):
            for p in d["posts"]:
                if p["id"] == m.group(1):
                    inc = {"users": [{"id": p["author_id"], "username": users.get(p["author_id"], "u")}]}
                    return self.send(200, {"data": post_json(p), "includes": inc})
            return self.send(404, {"errors": [{"title": "Not Found Error"}]})
        if path == "/2/tweets":
            return self.send(200, {"meta": {"result_count": 0}})
        return self.send(404, {"errors": [{"title": "Not Found Error"}]})


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()

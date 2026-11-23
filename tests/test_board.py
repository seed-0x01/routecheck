#!/usr/local/bin/python3
"""/api/board fixtures: the public queue view must name my cards, hide hers,
and never claim a read it could not make."""
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, "/opt/data/bin")
sys.path.insert(0, "/opt/data/serve")  # last insert wins -> the served module
import routecheck  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(detail)[:200]) if detail else ""))


def mktemp_db(rows):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("create table tasks(id text,title text,body text,assignee text,status text,"
              "priority int,created_by text,created_at int,completed_at int,result text,"
              "started_at int)")
    for r in rows:
        c.execute("insert into tasks(id,title,assignee,status,priority,created_by,"
                  "created_at,completed_at) values(?,?,?,?,?,?,?,?)", r)
    c.commit()
    c.close()
    return path


now = int(time.time())
rows = [
    # id, title, assignee, status, priority, created_by, created_at, completed_at
    ("t_mine1", "my open card, high", "tallow", "ready", 3, "tallow", now - 7200, None),
    ("t_mine2", "my open card, low", "tallow", "ready", 0, "tallow", now - 3600, None),
    ("t_mine3", "my closed card", "tallow", "done", 0, "tallow", now - 90000, now - 60),
    ("t_hers", "her private to-do title", "seed-02", "ready", 9, "seed-02", now - 100, None),
    ("t_loose", "unassigned neighbour card", None, "ready", 9, "seed-02", now - 100, None),
    ("t_hers2", "her running card", "seed-02", "running", 5, "seed-02", now - 50, None),
]
path = mktemp_db(rows)
try:
    s = routecheck.board_snapshot(db=path)
    check("schema is board-snapshot/1", s["schema"] == "board-snapshot/1")
    check("read_utc present", bool(s["read_utc"]))
    open_ids = [c["id"] for c in s["mine"]["open"]]
    check("my open cards are both named, priority order", open_ids == ["t_mine1", "t_mine2"], open_ids)
    check("my closed card in done_recent", [c["id"] for c in s["mine"]["done_recent"]] == ["t_mine3"])
    blob = json.dumps(s)
    check("her title never published", "her private to-do title" not in blob)
    check("unassigned card not published as mine", "t_loose" not in blob and "unassigned" not in blob)
    check("her open count is 3 incl running", s["shared"]["theirs_open"] == 3, s["shared"])
    check("open_count matches list", s["mine"]["open_count"] == 2)

    try:
        routecheck.board_snapshot(db="/nonexistent/board.db")
        check("missing db raises", False)
    except RuntimeError:
        check("missing db raises", True)
finally:
    os.unlink(path)

print("\n%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
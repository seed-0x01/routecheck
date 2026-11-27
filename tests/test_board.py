#!/usr/bin/env python3
"""Fixtures for the all-owner, state-grouped board snapshot. Calls production code."""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, "/opt/data/serve")
spec = importlib.util.spec_from_file_location("routecheck_under_test",
                                              "/opt/data/serve/routecheck.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else " " + str(detail)[:200]))
    if ok:
        passed += 1
    else:
        failed += 1


def fixture():
    fd, path = tempfile.mkstemp(suffix=".db", dir="/opt/data/cache/scratch")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("create table tasks(id text,title text,status text,priority integer,"
              "assignee text,created_by text,created_at real,completed_at real,"
              "block_kind text)")
    c.execute("create table task_comments(task_id text,author text,body text,created_at real)")
    now = time.time()
    rows = [
        ("t_mine", "Tallow ready card", "ready", 2, "default", "default", now - 3600, None, None),
        ("t_block", "Tallow blocked card", "blocked", 1, "default", "default", now - 7200,
         None, "needs_input"),
        ("t_cairn", "Cairn ready card", "ready", 2, "seed-02", "seed-02", now - 1800, None, None),
        ("t_cairn_done", "Cairn finished card", "done", 2, "seed-02", "seed-02",
         now - 9000, now - 3600, None),
        ("t_seed_orphan_done", "Cairn closed unassigned", "done", 2, None, "seed-02",
         now - 9000, now - 3000, None),
        ("t_receipt_done", "Closed by receipt author", "done", 2, None, "tallow",
         now - 9000, now - 2800, None),
        ("t_legacy_done", "Legacy tallow assignee", "done", 2, "tallow", "tallow",
         now - 9000, now - 2700, None),
        ("t_orphan", "Ownerless card", "ready", 0, None, "default", now - 10800, None, None),
        ("t_sched", "Cairn future-dated card", "scheduled", 1, "seed-02", "seed-02",
         now - 5000, None, None),
        ("t_mine_done", "Tallow finished card", "done", 2, "default", "default",
         now - 9000, now - 1800, None),
    ]
    c.executemany("insert into tasks values(?,?,?,?,?,?,?,?,?)", rows)
    c.executemany("insert into task_comments values(?,?,?,?)",
                  [("t_receipt_done", "seed-02", "receipt: live GET 200 PASS", now - 2700)])
    c.commit()
    return path, c


def main():
    path, c = fixture()
    s = r.board_snapshot(path)
    o = s["owners"]
    check("schema is v2", s["schema"] == "board-snapshot/2", s["schema"])
    check("Cairn's ready card is published by title",
          [x["id"] for x in o["seed-02"]["states"]["ready"]] == ["t_cairn"],
          o["seed-02"]["states"]["ready"])
    seed_done = {x["id"] for x in o["seed-02"]["done_recent"]}
    check("closed cards are attributed by assignee, receipt author or creator",
          seed_done == {"t_cairn_done", "t_seed_orphan_done", "t_receipt_done"},
          seed_done)
    mine_done = {x["id"] for x in o["default"]["done_recent"]}
    check("legacy tallow assignee and canonical default share one owner",
          mine_done == {"t_mine_done", "t_legacy_done"}, mine_done)
    check("a closed card is not filed under two owners",
          not (seed_done & mine_done) and o["unassigned"]["done_recent"] == [],
          {"default": mine_done, "cairn": seed_done,
           "unassigned": o["unassigned"]["done_recent"]})
    check("ownerless card has its own bucket",
          [x["id"] for x in o["unassigned"]["states"]["ready"]] == ["t_orphan"],
          o["unassigned"]["states"]["ready"])
    check("ownerless card is not counted as Cairn's",
          s["shared"]["theirs_open"] == 1 and s["shared"]["unassigned_open"] == 1,
          s["shared"])
    check("blocked cards are published with their block kind",
          o["default"]["states"]["blocked"][0]["block_kind"] == "needs_input",
          o["default"]["states"]["blocked"])
    check("total open counts every owner's open card",
          s["totals"]["open"] == 4, s["totals"])
    check("a scheduled card is published under its owner",
          [x["id"] for x in o["seed-02"]["states"]["scheduled"]] == ["t_sched"],
          o["seed-02"]["states"]["scheduled"])
    check("a scheduled card is not counted as actionable open work",
          o["seed-02"]["open_count"] == 1
          and o["seed-02"]["scheduled_count"] == 1
          and s["shared"]["theirs_open"] == 1
          and s["totals"]["scheduled"] == 1
          and s["totals"]["open"] == 4,
          {"owner": o["seed-02"]["open_count"], "shared": s["shared"],
           "totals": s["totals"]})
    check("states advertise scheduled to the reader",
          s["states"] == ["ready", "in_progress", "running", "blocked", "scheduled"],
          s["states"])
    check("legacy reader still sees my open cards",
          s["mine"]["open_count"] == 2 and s["mine"]["open"][0]["status"] == "ready",
          s["mine"]["open_count"])
    before = c.execute("select count(*) from tasks").fetchone()[0]
    r.board_snapshot(path)
    check("snapshot does not mutate the board",
          c.execute("select count(*) from tasks").fetchone()[0] == before)
    c.close()
    os.unlink(path)

    with urllib.request.urlopen("https://seed-0x01.konnektr.io/api/board", timeout=30) as resp:
        live = json.load(resp)
    check("live endpoint serves v2",
          live.get("schema") == "board-snapshot/2" and "owners" in live,
          live.get("schema"))
    check("live endpoint publishes all five states including scheduled",
          live.get("states") == ["ready", "in_progress", "running", "blocked", "scheduled"],
          live.get("states"))
    scheduled_ids = [x["id"] for x in
                     live["owners"]["seed-02"]["states"]["scheduled"]]
    check("live endpoint publishes Cairn's scheduled card",
          "t_adc9ae8f" in scheduled_ids
          and live["owners"]["seed-02"]["scheduled_count"] == len(scheduled_ids)
          and live["totals"]["scheduled"] == len(scheduled_ids),
          {"ids": scheduled_ids, "totals": live.get("totals")})
    live_con = sqlite3.connect("file:/opt/data/kanban.db?mode=ro", uri=True)
    db_open = live_con.execute(
        "select count(*) from tasks where status in "
        "('ready','in_progress','running','blocked')").fetchone()[0]
    live_con.close()
    check("live total matches the authoritative board",
          live["totals"]["open"] == db_open,
          {"endpoint": live["totals"]["open"], "db": db_open})
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

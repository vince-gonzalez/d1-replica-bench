# ============================================================
# D1-REPLICA-BENCH DRIVER v0.1.0
# Measures Cloudflare D1 read-replica lag: write a new value to the primary, then
# poll an UNCONSTRAINED read (which a replica may answer) until the new value
# appears -- timing, from an external wall clock, how long the stale value lasts.
# F-Keys LLC | www.f-keys.com
# ============================================================
# PER-TRIAL PROTOCOL
#   1. POST /d1/write?v=NEW   -- first-primary session; the authoritative write.
#   2. t0 = wall clock.
#   3. poll GET /d1/read      -- unconstrained; record the value, the serving
#      region, and served_by_primary, until val == NEW or timeout.
#      -> flip latency = (first time NEW is read) - t0.
#   Each poll records who served it: a poll answered by the PRIMARY shows NEW at
#   once (no lag); a poll answered by a lagging REPLICA is where the lag lives.
#
# TIMING AUTHORITY: time.monotonic() in THIS process, outside the edge.
# STDLIB ONLY. Browser User-Agent (workers.dev rejects Python-urllib).
#
# CONFIG (env): BENCH_BASE, BENCH_TOKEN,
#   D1_N (trials, default 20), D1_POLL (s, default 0.2),
#   D1_FLIP_TIMEOUT (s, default 20), D1_OUT (default d1_lag.json).
# ============================================================

import json
import os
import sys
import time
import uuid
import statistics
import urllib.request
import urllib.parse
import urllib.error

BASE = os.environ.get("BENCH_BASE", "").rstrip("/")
TOKEN = os.environ.get("BENCH_TOKEN", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
N = int(os.environ.get("D1_N", "20"))
POLL = float(os.environ.get("D1_POLL", "0.2"))
FLIP_TIMEOUT = float(os.environ.get("D1_FLIP_TIMEOUT", "20"))
OUT = os.environ.get("D1_OUT", "d1_lag.json")


def call(path, method):
    url = BASE + path
    req = urllib.request.Request(
        url, method=method,
        headers={"User-Agent": UA, "X-Bench-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def run():
    if not BASE or not TOKEN:
        sys.exit("Set BENCH_BASE and BENCH_TOKEN.")
    init = call("/d1/init", "POST")
    print("init: %s" % json.dumps(init))
    run_id = uuid.uuid4().hex[:8]
    results = {"run_id": run_id, "base": BASE, "n": N, "trials": []}
    reads = 0
    for i in range(N):
        newv = uuid.uuid4().hex
        w = call("/d1/write?v=%s" % urllib.parse.quote(newv), "POST")
        t0 = time.monotonic()
        polls = []
        flip = None
        while True:
            t = time.monotonic() - t0
            try:
                r = call("/d1/read", "GET")
            except Exception as e:
                r = {"val": None, "served_by_region": "ERR", "served_by_primary": None}
                print("  read error: %s" % e)
            reads += 1
            is_new = (r.get("val") == newv)
            polls.append({"t": round(t, 3), "new": is_new,
                          "primary": r.get("served_by_primary"),
                          "region": r.get("served_by_region")})
            if is_new:
                flip = t
                break
            if t >= FLIP_TIMEOUT:
                break
            time.sleep(POLL)
        # was the stale value ever served by a replica (not the primary)?
        saw_replica_stale = any((p["new"] is False and p["primary"] is False) for p in polls)
        regions = sorted(set(p["region"] for p in polls if p["region"]))
        results["trials"].append({
            "i": i, "write_region": w.get("served_by_region"),
            "write_primary": w.get("served_by_primary"),
            "flip_s": flip, "saw_replica_stale": saw_replica_stale,
            "regions": regions, "polls": polls,
        })
        fstr = ("%.3f" % flip) if flip is not None else "TO"
        print("  trial %2d/%d: write@%s flip=%ss replica_stale=%s regions=%s"
              % (i + 1, N, w.get("served_by_region"), fstr, saw_replica_stale, ",".join(regions)))

    # --- aggregate: lag only counts trials where a replica actually served stale ---
    lagged = [t["flip_s"] for t in results["trials"]
              if t["saw_replica_stale"] and t["flip_s"] is not None]
    primary_only = sum(1 for t in results["trials"] if not t["saw_replica_stale"])
    print("")
    print("=== D1 read-replica lag ===")
    if lagged:
        vs = sorted(lagged)
        p90 = vs[min(len(vs) - 1, int(round(0.9 * (len(vs) - 1))))]
        print("  replica-served-stale trials: n=%d | min=%.3f med=%.3f p90=%.3f max=%.3f (s)"
              % (len(vs), vs[0], statistics.median(vs), p90, vs[-1]))
    else:
        print("  no trial was served a stale value by a replica (reads may be")
        print("  routing to the primary from this location).")
    print("  trials where only the primary served: %d / %d" % (primary_only, N))

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(results, f, indent=2)
    writes = N + 1
    print("")
    print("budget: writes=%d (vs 100k/day free), reads=%d (vs 5M/day free). Wrote %s"
          % (writes, reads, OUT))


if __name__ == "__main__":
    run()

# ============================================================
# D1-REPLICA-BENCH CROSS-REGION DRIVER v0.3.0
# Measures Cloudflare D1 read-replica lag ACROSS REGIONS: write a new value to the
# primary, then measure how long a replica in another region keeps serving the old
# value. One region is measured at a time, TIGHT-POLLED (no interleaving), so a
# region's lag is not contaminated by the latency of polling other regions.
# F-Keys LLC | www.f-keys.com
# ============================================================
# PER-(REGION,TRIAL) PROTOCOL
#   1. POST /d1/write?v=NEW             -- first-primary session; authoritative write.
#   2. t0 = wall clock.
#   3. tight loop GET /d1/rread?region=R (no sleep) until its served value == NEW.
#      -> lag = (first time NEW is served) - t0.
#   The FIRST poll's round-trip is recorded as the resolution FLOOR: a true lag
#   smaller than one read round-trip to region R cannot be distinguished from it.
#   served_by_primary is recorded; a region answered by the PRIMARY is not a replica
#   lag and is reported separately.
#
# A warm-up runs first (D1 auto-mode provisions replicas in response to regional
# read traffic), and each region is re-warmed right before its trials.
#
# TIMING AUTHORITY: time.monotonic() in THIS process, outside the edge.
# STDLIB ONLY. Browser User-Agent (workers.dev rejects Python-urllib).
#
# CONFIG (env): BENCH_BASE, BENCH_TOKEN,
#   D1_REGIONS (default the 10 documented hints),
#   D1_N (trials per region, default 8), D1_FLIP_TIMEOUT (s, default 20),
#   D1_WARM (warm rounds, default 3), D1_OUT (default d1_mr_lag.json).
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
REGIONS = [r.strip() for r in os.environ.get(
    "D1_REGIONS", "wnam,weur,eeur,apac,apac-ne,apac-se,oc,sam,afr,me").split(",") if r.strip()]
N = int(os.environ.get("D1_N", "8"))
FLIP_TIMEOUT = float(os.environ.get("D1_FLIP_TIMEOUT", "20"))
WARM = int(os.environ.get("D1_WARM", "3"))
OUT = os.environ.get("D1_OUT", "d1_mr_lag.json")


def call(path, method):
    url = BASE + path
    req = urllib.request.Request(
        url, method=method, headers={"User-Agent": UA, "X-Bench-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def write(v):
    return call("/d1/write?v=%s" % urllib.parse.quote(v), "POST")


def rread(region):
    return call("/d1/rread?region=%s" % urllib.parse.quote(region), "GET")


def run():
    if not BASE or not TOKEN:
        sys.exit("Set BENCH_BASE and BENCH_TOKEN.")
    call("/d1/init", "POST")
    print("warming replicas (%d rounds x %d regions)..." % (WARM, len(REGIONS)))
    for _ in range(WARM):
        for r in REGIONS:
            try:
                rread(r)
            except Exception:
                pass

    run_id = uuid.uuid4().hex[:8]
    results = {"run_id": run_id, "base": BASE, "regions": REGIONS,
               "n_per_region": N, "by_region": {}, "trials": []}
    reads = WARM * len(REGIONS)
    writes = 0

    for R in REGIONS:
        # a couple of warm reads for this region, and learn who serves it
        try:
            rread(R)
            s = rread(R)
            reads += 2
        except Exception:
            s = {}
        served_region = s.get("served_by_region")
        served_primary = s.get("served_by_primary")

        lags = []
        floors = []
        replica_count = 0
        for _ in range(N):
            newv = uuid.uuid4().hex
            write(newv)
            writes += 1
            t0 = time.monotonic()
            first = None
            lag = None
            replica_seen = False
            while True:
                resp = None
                try:
                    resp = rread(R)
                except Exception:
                    pass
                reads += 1
                now = time.monotonic()
                if first is None:
                    first = now - t0
                if resp is not None:
                    if resp.get("served_by_primary") is False:
                        replica_seen = True
                    if resp.get("val") == newv:
                        lag = now - t0
                        break
                if now - t0 >= FLIP_TIMEOUT:
                    break
            if replica_seen:
                replica_count += 1
            if lag is not None:
                lags.append(lag)
            if first is not None:
                floors.append(first)
            results["trials"].append({"region": R, "lag_s": lag,
                                       "first_poll_s": first, "replica_seen": replica_seen})

        info = {"served_region": served_region, "served_primary": served_primary,
                "n": len(lags), "replica_trials": replica_count,
                "floor_s": round(min(floors), 3) if floors else None}
        if lags:
            vs = sorted(lags)
            info["min"] = round(vs[0], 3)
            info["median"] = round(statistics.median(vs), 3)
            info["p90"] = round(vs[min(len(vs) - 1, int(round(0.9 * (len(vs) - 1))))], 3)
            info["max"] = round(vs[-1], 3)
        results["by_region"][R] = info
        if lags:
            print("  %-8s served_by=%s primary=%s | n=%d replica=%d floor=%.2f | med=%.2f p90=%.2f max=%.2f"
                  % (R, served_region, served_primary, len(lags), replica_count,
                     info["floor_s"], info["median"], info["p90"], info["max"]))
        else:
            print("  %-8s served_by=%s primary=%s | no flips measured" % (R, served_region, served_primary))

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(results, f, indent=2)
    print("")
    print("budget: writes=%d (vs 100k/day), reads=%d (vs 5M/day). Wrote %s"
          % (writes, reads, OUT))


if __name__ == "__main__":
    run()

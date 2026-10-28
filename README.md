# D1-Replica-Bench

Measures two things Cloudflare D1 read replication documents only in words:
**after a write to the primary, how long can a read from a replica in another
region still return the old value — and when you enable replication, are the
replicas there yet?**

## What it measures

- **Provisioning.** Right after enabling `read_replication = auto`, which regions
  actually serve from a replica (`served_by_primary: false`) versus still falling
  back to the primary.
- **Cross-region lag.** Write a new value at the primary (`withSession("first-primary")`),
  then measure how long a replica in each region keeps serving the old value from
  an *unconstrained* `withSession()` read. Each region is pinned with a Durable
  Object `locationHint` and reports its real colo, so placement is verified.

## The headline finding

- Replicas are **provisioned by traffic, not by the toggle**: a freshly-enabled
  `auto` database serves every region from the primary until regional read traffic
  arrives (then replicas appear within ~a minute).
- Once serving, the stale window is **below one read round-trip** (~0.1–0.35 s by
  region). Across 100 trials in 10 regions, no stale read was caught. See
  [`results/FINDINGS.md`](results/FINDINGS.md).
- For contrast, Workers KV on the same account shows a ~53 s cross-region stale
  window; D1 read replication is three orders of magnitude tighter.

## Honest method note

A first driver that polled all regions in one interleaved loop produced apparent
0.4–2.7 s lags that were an **artifact of the poll schedule**, not replication.
The real measurement polls **one region at a time, tight** (`drive_mr.py`), which
collapses every number to the round-trip floor. Don't interleave regions in one
timed loop.

## What it does NOT claim

- One database, one account, one time window, free tier, 100 trials.
- The floor is the read round trip; a true lag below it is unresolvable — the
  result is "no stale read observed beyond the floor," not "zero."

## Run it

1. Enable read replication on your database (dashboard **Settings → Enable Read
   Replication**, or `PUT .../d1/database/{id}` with `{"read_replication":{"mode":"auto"}}`).
2. Put the database id/name in `wrangler.toml` (use a throwaway copy — the committed
   file keeps placeholders on purpose) and `wrangler deploy`.
3. Set the gate secret: `wrangler secret put BENCH_TOKEN`
4. Point the driver at the Worker and run it:
   - `BENCH_BASE` = the Worker origin
   - `BENCH_TOKEN` = the same secret
   - `python drive_mr.py`  (cross-region lag; `drive.py` is a single-region baseline)

Budget: 100 writes and a few hundred reads — against the free-tier 100k writes/day
and 5M reads/day, under 0.2% of either.

F-Keys LLC | www.f-keys.com

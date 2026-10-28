# D1-Replica-Bench Findings v0.1.0

Cloudflare D1 read replication (Sessions API), Workers **free** tier, 2026-10-28.
One database (`f-keys-d1bench`), primary in ENAM. Raw data: `d1_mr_lag.json`
(run of 10 regions × 10 trials, tight per-region polling).

## Question

D1 read replication is documented as asynchronous: an *unconstrained* read (a
`withSession()` with no bookmark) "may be served by a replica" and can therefore
return stale data, and the docs define "replica lag" — but publish no number. Two
questions: **how long is the stale window after a write, across regions? And when
you enable replication, are replicas there immediately?**

## The finding

**1. Replicas are provisioned by traffic, not by the toggle.** Right after setting
`read_replication = auto`, every unconstrained read — from readers pinned to all
10 regions (verified by colo: SJC, AMS, WAW, SIN, ICN, SYD, …) — was still served
by the **primary** in ENAM (`served_by_primary: true`). Zero replicas served. After
a burst of ~120 cross-region reads, replicas appeared within about a minute and
began serving WNAM, WEUR, EEUR, APAC, OC, etc. (`served_by_primary: false`). So a
freshly-enabled database has **no serving replicas until regional read traffic
arrives** — a cold `auto` database behaves like an un-replicated one.

**2. Once a replica is serving, the stale window is below one read round-trip.**
Measured one region at a time, tight-polled, 10 trials each:

| hint | served from | median lag (s) | floor (s) | in-region replica? |
|---|---|---|---|---|
| wnam | WNAM | 0.18 | 0.17 | yes |
| weur | WEUR | 0.24 | 0.23 | yes |
| eeur | EEUR | 0.23 | 0.22 | yes |
| apac | APAC | 0.38 | 0.35 | yes |
| apac-ne | APAC | 0.30 | 0.29 | nearest |
| apac-se | APAC | 0.32 | 0.31 | nearest |
| oc | OC | 0.33 | 0.31 | yes |
| sam | ENAM | 0.13 | 0.12 | nearest (no SA replica) |
| afr | WEUR | 0.24 | 0.22 | nearest (no African replica) |
| me | EEUR | 0.24 | 0.23 | nearest (no ME replica) |

In **every** region the median lag equals the measurement **floor** — the time for
a single read to make the round trip to that replica. By the moment the first read
arrives, the replica already holds the new value. Across 100 trials in 10 regions
**not one stale read was caught.** The practical staleness window for an
unconstrained read after a `first-primary` write is therefore **under ~0.1–0.35 s**
(one cross-region round trip); any true lag below that is smaller than this
harness can resolve. It is *not* zero — only below the floor.

**3. D1 replicas land in the same displaced regions as Durable Object
`locationHint`.** `sam` was served from ENAM, `afr` from WEUR, `me` from EEUR —
i.e. there is no replica in South America, Africa, or the Middle East, and the
nearest one answers. This matches the DO `locationHint` placement finding exactly
(see the companion `locationhint-bench`): those three regions have no edge
presence for this workload and resolve to the US-East / Western-Europe substitutes.

## Method — and an honest correction

- **Write at the primary:** `env.DB.withSession("first-primary")` so the write is
  on the source of truth; the read is a separate request with a fresh
  `env.DB.withSession()` (no bookmark), so a replica may answer.
- **Reads issued from each region** by a Durable Object pinned with `locationHint`,
  which reports its real colo (cdn trace) and D1's `served_by_region` /
  `served_by_primary` — so placement is verified, not assumed.
- **Timed from the driver's wall clock** (`time.monotonic()`), outside the edge.
- **A measurement error, documented so others avoid it.** A first driver polled all
  10 regions in a fixed round with a sleep between rounds. It produced tidy,
  reproducible per-region "lags" of 0.4–2.7 s — which were an **artifact of the
  poll schedule** (each region's flip time was entangled with its position in the
  round and the latency of the reads before it), not replication delay. Switching
  to one-region-at-a-time tight polling collapsed every number to the round-trip
  floor. If you benchmark this, do not interleave regions in one timed loop.

## What this does NOT claim

- One database, one account, **one time window**, free tier, 100 trials. Measured
  immediately after enabling replication (replicas freshly warmed).
- The lag floor is the read round trip (~0.1–0.35 s by region); a true lag below
  that is unresolvable here. The result is "no stale read observed beyond the
  floor," not "zero."
- `first-primary` write path and unconstrained reads only; bookmark-constrained
  reads (the normal Sessions API path) are current by construction and were not the
  subject.
- Replica placement reflects this account/workload at this time; D1 `auto` may
  place replicas differently under different traffic.

## Relation to the Cloudflare docs

The docs are correct: unconstrained reads *may* be stale, and replica lag exists.
This is not a correction. The contribution is the measured magnitude — the stale
window is sub-round-trip in practice, not a figure the docs give — plus two
behaviours the docs do not spell out: replicas are **provisioned by traffic** (a
cold `auto` DB serves only from the primary), and `sam`/`afr`/`me` have no local
replica. For contrast, Cloudflare **Workers KV** on the same account shows a
cross-region stale window of ~53 s (one `cacheTtl`); D1 read replication, by a
different mechanism (active replication, not passive cache expiry), is three orders
of magnitude tighter.

## For a developer, in one line

An unconstrained D1 read after your own primary write is current within a round
trip anywhere in the world — but a freshly-enabled `auto` database serves only
from the primary until regional traffic provisions its replicas.

F-Keys LLC | www.f-keys.com

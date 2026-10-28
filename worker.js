// ============================================================
// D1-REPLICA-BENCH v0.2.0
// Measures Cloudflare D1 READ-REPLICA LAG: after a write commits on the primary,
// how long does an unconstrained read served by a replica in another region keep
// returning the OLD value? The D1 docs define "replica lag" but publish no number.
// F-Keys LLC | www.f-keys.com
// ============================================================
//
// CHANGE LOG v0.1.0 -> v0.2.0
// - Added class D1RegionReader (a Durable Object) + GET /d1/rread?region=R: pin a
//   reader to region R via locationHint and run the UNCONSTRAINED read from there,
//   so it is answered by the replica nearest R -- the only way to see cross-region
//   lag. A single-region read near the primary is always served by the primary.
// - Kept /d1/read (single-region, near the driver) as a primary-side baseline.
//
// WORKFLOW STACK:
// 1. Gate every request on BENCH_TOKEN (X-Bench-Token header or ?token=).
// 2. POST /d1/init           -> create the single-row table and seed it (primary).
// 3. POST /d1/write?v=VAL    -> first-primary session UPDATE; the authoritative write.
// 4. GET  /d1/read           -> UNCONSTRAINED read from the driver's own region.
// 5. GET  /d1/rread?region=R -> UNCONSTRAINED read from a DO pinned to region R;
//                               returns the value, the colo the reader ran in, the
//                               region D1 served it from, and served_by_primary.
//
// ASSET MANIFEST:
// - D1 binding: DB  (read replication enabled on the database)
// - DO binding: READER -> class D1RegionReader (sqlite-class migration, no storage used)
// - Secret:     BENCH_TOKEN
//
// BOOT ORDER:
// - Stateless coordinator. One D1RegionReader per region, pinned by locationHint;
//   each is just a regional anchor for an unconstrained D1 read.
// ============================================================

import { DurableObject } from "cloudflare:workers";

export class D1RegionReader extends DurableObject {
	// read: run an UNCONSTRAINED D1 read from THIS DO's region (so a replica may
	// answer) and report the value plus who served it and the colo we ran in.
	async read() {
		try {
			var s = this.env.DB.withSession();
			var rr = await s.prepare("SELECT val FROM lag WHERE id=1").all();
			var row = (rr.results && rr.results.length) ? rr.results[0] : null;
			var mr = rr.meta || {};
			var where = await this.colo();
			return {
				val: row ? row.val : null,
				colo: where,
				served_by_region: mr.served_by_region || null,
				served_by_primary: mr.served_by_primary === undefined ? null : mr.served_by_primary
			};
		} catch (err) {
			console.error("D1RegionReader.read:", err);
			return { val: null, colo: "err", served_by_region: null, served_by_primary: null };
		}
	}

	// colo: learn once which colo this DO runs in, via a cdn trace. Memoized in memory.
	async colo() {
		try {
			if (this.coloCache) { return this.coloCache; }
			var resp = await fetch("https://workers.cloudflare.com/cdn-cgi/trace");
			var text = await resp.text();
			var m = text.match(/colo=([A-Z]+)/);
			this.coloCache = m ? m[1] : "unknown";
			return this.coloCache;
		} catch (err) {
			console.error("D1RegionReader.colo:", err);
			return "unknown";
		}
	}
}

export default {
	async fetch(request, env) {
		try {
			var url = new URL(request.url);
			var path = url.pathname;

			var supplied = request.headers.get("x-bench-token") || url.searchParams.get("token") || "";
			if (!env.BENCH_TOKEN || supplied !== env.BENCH_TOKEN) {
				return json({ error: "forbidden" }, 403);
			}

			if (path === "/d1/init" && request.method === "POST") {
				var sInit = env.DB.withSession("first-primary");
				await sInit.prepare("CREATE TABLE IF NOT EXISTS lag (id INTEGER PRIMARY KEY, val TEXT, ts INTEGER)").run();
				await sInit.prepare("INSERT INTO lag (id, val, ts) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET val=excluded.val, ts=excluded.ts").bind("init", Date.now()).run();
				return json({ ok: true, bookmark: sInit.getBookmark() });
			}

			if (path === "/d1/write" && request.method === "POST") {
				var wval = url.searchParams.get("v") || "";
				if (!wval) { return json({ error: "missing v" }, 400); }
				// first-primary: the write lands on the primary, the source of truth.
				var sW = env.DB.withSession("first-primary");
				var rw = await sW.prepare("UPDATE lag SET val=?, ts=? WHERE id=1").bind(wval, Date.now()).run();
				var mw = rw.meta || {};
				return json({
					ok: true,
					served_by_region: mw.served_by_region || null,
					served_by_primary: mw.served_by_primary === undefined ? null : mw.served_by_primary,
					bookmark: sW.getBookmark()
				});
			}

			if (path === "/d1/read" && request.method === "GET") {
				// Unconstrained read from the driver's own region (primary-side baseline).
				var sR = env.DB.withSession();
				var rr = await sR.prepare("SELECT val FROM lag WHERE id=1").all();
				var row = (rr.results && rr.results.length) ? rr.results[0] : null;
				var mr = rr.meta || {};
				return json({
					val: row ? row.val : null,
					served_by_region: mr.served_by_region || null,
					served_by_primary: mr.served_by_primary === undefined ? null : mr.served_by_primary
				});
			}

			if (path === "/d1/rread" && request.method === "GET") {
				var region = url.searchParams.get("region") || "";
				if (!region) { return json({ error: "missing region" }, 400); }
				var id = env.READER.idFromName("rdr-" + region);
				var stub = env.READER.get(id, { locationHint: region });
				var out = await stub.read();
				return json({
					region: region, colo: out.colo, val: out.val,
					served_by_region: out.served_by_region,
					served_by_primary: out.served_by_primary
				});
			}

			return json({ error: "not found" }, 404);
		} catch (err) {
			console.error("fetch:", err);
			return json({ error: "internal", detail: String(err) }, 500);
		}
	}
};

// json: never cached, so only D1 timing is measured, never an HTTP cache.
function json(obj, status) {
	return new Response(JSON.stringify(obj), {
		status: status || 200,
		headers: { "content-type": "application/json", "cache-control": "no-store" }
	});
}

#!/usr/bin/env python3
"""Create a Zenodo deposition for this repository, and STOP before publishing.

    python -I deposit.py            # create or update the draft
    python -I deposit.py --publish  # mint the DOI, irreversible

WHY IT DOES NOT PUBLISH BY DEFAULT
A published Zenodo record cannot be withdrawn. The DOI is permanent, it
resolves forever, and the metadata is harvested by indexers within the day. So
this builds the draft, uploads the archive, prints the link to read it in a
browser, and exits. Publishing is a separate explicit act by a person who has
read the thing.

WHAT IT UPLOADS
A source archive of the tagged release taken from `git archive`, so the bytes
deposited are the bytes the tag names rather than whatever is in the working
directory. The findings document goes up alongside it as a separate file, so
the result is readable from the record page without downloading anything.

ZENODO_TOKEN must be in the environment. It is never printed.
"""
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "https://zenodo.org/api"
TOKEN = os.environ.get("ZENODO_TOKEN")
if not TOKEN:
    sys.exit("ZENODO_TOKEN is not set")

HERE = os.path.dirname(os.path.abspath(__file__))
TAG = "v0.1.0"
ARCHIVE = os.path.join(HERE, "d1-replica-bench-%s.zip" % TAG)
PUBLISH = "--publish" in sys.argv

METADATA = {
    "metadata": {
        "upload_type": "software",
        "title": ("D1-Replica-Bench: measured read-replica lag and "
                  "traffic-provisioning for Cloudflare D1"),
        "creators": [{
            "name": "Gonzalez, Vincent",
            "orcid": "0009-0005-3640-014X",
        }],
        "license": "mit",
        "version": TAG,
        "language": "eng",
        "keywords": [
            "Cloudflare D1", "read replication", "Sessions API", "replica lag",
            "eventual consistency", "edge database", "serverless",
            "reproducible measurement",
        ],
        "related_identifiers": [{
            "identifier": "https://github.com/vince-gonzalez/d1-replica-bench",
            "relation": "isSupplementTo",
            "scheme": "url",
        }],
        "description": """
<p>Cloudflare D1 read replication serves reads from regional replicas. An
unconstrained Sessions-API read "may be served by a replica" and can return stale
data; the docs define "replica lag" but publish no number. This harness measures
it on the free tier, from both sides.</p>

<p><b>Replicas are provisioned by traffic, not by the toggle.</b> Immediately after
enabling <code>read_replication = auto</code>, unconstrained reads from readers
pinned to all 10 regions were still served by the primary (ENAM). After a burst of
~120 cross-region reads, replicas appeared within about a minute and began serving
each region. A cold <code>auto</code> database behaves like an un-replicated one
until regional read traffic arrives.</p>

<p><b>Once serving, the stale window is below one read round-trip.</b> Measured one
region at a time, tight-polled, 10 trials each across 10 regions, the median lag in
every region equalled the measurement floor (the read round trip, 0.1&ndash;0.35&nbsp;s).
Across 100 trials no stale read was caught: by the time the first read reaches a
replica, it already holds the new value. For contrast, Workers KV on the same
account shows a ~53&nbsp;second cross-region stale window.</p>

<p><b>Method, with a documented correction.</b> Writes use
<code>withSession("first-primary")</code>; reads use an unconstrained
<code>withSession()</code> from a Durable Object pinned per region, reporting
<code>served_by_region</code> / <code>served_by_primary</code> and the real colo.
Timing is the driver's external wall clock. A first driver that polled all regions
in one interleaved loop produced apparent 0.4&ndash;2.7&nbsp;s lags that were an
artifact of the poll schedule, not replication; one-region-at-a-time tight polling
collapsed them to the round-trip floor. Stated limits: one database, one account,
one time window, free tier; a true lag below the round-trip floor is unresolvable,
so the result is "no stale read observed beyond the floor," not "zero."</p>
""".strip(),
    }
}


def req(method, path, body=None, headers=None, raw=None):
    url = path if path.startswith("http") else API + path
    sep = "&" if "?" in url else "?"
    url = "%s%saccess_token=%s" % (url, sep, TOKEN)
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None)
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            txt = resp.read().decode()
            return resp.status, (json.loads(txt) if txt else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"raw": txt[:500]}


def say(label, value):
    print("  %-22s %s" % (label, value))


# --- the archive is built from the TAG, not the working directory ------
subprocess.run(["git", "archive", "--format=zip",
                "--prefix=d1-replica-bench-%s/" % TAG,
                "-o", ARCHIVE, TAG],
               cwd=HERE, check=True)
say("archive", "%s (%d bytes, from tag %s)"
    % (os.path.basename(ARCHIVE), os.path.getsize(ARCHIVE), TAG))

# --- reuse an existing unpublished draft rather than making a second --
status, mine = req("GET", "/deposit/depositions?status=draft&size=100")
draft = None
if status == 200 and isinstance(mine, list):
    for d in mine:
        if d.get("title", "").startswith("D1-Replica-Bench:"):
            draft = d
            break

if draft:
    say("existing draft", draft["id"])
    dep_id = draft["id"]
    status, body = req("PUT", "/deposit/depositions/%s" % dep_id, METADATA)
    if status not in (200, 201):
        sys.exit("  metadata update failed: %s %s" % (status, body))
else:
    status, body = req("POST", "/deposit/depositions", METADATA)
    if status not in (200, 201):
        sys.exit("  could not create the deposition: %s %s" % (status, body))
    draft = body
    dep_id = body["id"]
    say("created draft", dep_id)

status, draft = req("GET", "/deposit/depositions/%s" % dep_id)
bucket = draft["links"]["bucket"]

# --- upload, replacing any same-named file from an earlier attempt ----
for existing in draft.get("files", []):
    req("DELETE", "/deposit/depositions/%s/files/%s"
        % (dep_id, existing["id"]))

for path, name in [(ARCHIVE, os.path.basename(ARCHIVE)),
                   (os.path.join(HERE, "results", "FINDINGS.md"),
                    "FINDINGS.md")]:
    with io.open(path, "rb") as fh:
        blob = fh.read()
    st, resp = req("PUT", "%s/%s" % (bucket, name), raw=blob,
                   headers={"content-type": "application/octet-stream"})
    say("uploaded" if st in (200, 201) else "UPLOAD FAILED %s" % st,
        "%s (%d bytes)" % (name, len(blob)))

status, draft = req("GET", "/deposit/depositions/%s" % dep_id)
print()
say("state", draft.get("state"))
say("submitted", draft.get("submitted"))
say("files", ", ".join(f["filename"] for f in draft.get("files", [])))
say("reserved DOI", draft.get("metadata", {}).get(
    "prereserve_doi", {}).get("doi", "(assigned on publish)"))
print()
print("  READ IT HERE before anything is permanent:")
print("    %s" % draft["links"].get("html", "(no html link returned)"))

if not PUBLISH:
    print()
    print("  Not published. The draft is private to the account and nothing")
    print("  has a DOI yet. To mint it:  python -I deposit.py --publish")
    sys.exit(0)

print()
print("  PUBLISHING - this is irreversible")
status, body = req("POST", "/deposit/depositions/%s/actions/publish" % dep_id)
if status not in (200, 202):
    sys.exit("  publish failed: %s %s" % (status, body))
say("published DOI", body.get("doi"))
say("record", body.get("links", {}).get("record_html"))

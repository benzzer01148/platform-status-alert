"""End-to-end test with a fake Statuspage + fake LINE API (no network).
Run:  python -I tests/test_monitor.py
"""
import json, os, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
MONITOR = os.path.join(HERE, "..", "monitor.py")

DATA = {"summary": None, "incidents": None, "json_ok": True, "rss": "", "atom": "", "atom_ok": True, "line_fail": False}
PUSHED = []
GCHAT = []


def comp(cid, name, status):
    return {"id": cid, "name": name, "status": status, "group": False}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if (self.path.startswith("/api/v2/") and not DATA["json_ok"]) or (self.path == "/history.atom" and not DATA["atom_ok"]):
            self.send_response(404); self.end_headers(); return
        body = {
            "/api/v2/summary.json": lambda: json.dumps(DATA["summary"]),
            "/api/v2/incidents.json": lambda: json.dumps({"incidents": DATA["incidents"]}),
            "/history.rss": lambda: DATA["rss"],
            "/history.atom": lambda: DATA["atom"],
        }.get(self.path)
        if not body:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.end_headers(); self.wfile.write(body().encode())

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(n))
        if self.path.startswith("/gchat"):
            if DATA.get("gchat_fail"):
                self.send_response(500); self.end_headers(); return
            GCHAT.append(payload["text"]); self.send_response(200); self.end_headers(); self.wfile.write(b"{}"); return
        assert self.headers["Authorization"] == "Bearer test-token"
        if DATA["line_fail"]:
            self.send_response(500); self.end_headers(); return
        PUSHED.append((self.path, payload))
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
tmp = tempfile.mkdtemp()
env = dict(os.environ, STATUS_BASE_URL=base, LINE_API_BASE=base,
           LINE_CHANNEL_ACCESS_TOKEN="test-token", LINE_TO="Uxxxx",
           STATE_FILE=os.path.join(tmp, "state.json"))


def run():
    PUSHED.clear(); GCHAT.clear()
    p = subprocess.run([sys.executable, "-I", MONITOR], env=env, capture_output=True, text=True)
    return p.returncode, [x[1]["messages"][0]["text"] for x in PUSHED], p


def upd(uid, status, t, body):
    return {"id": uid, "status": status, "created_at": t, "body": body}


ok_comps = [comp("c1", "API", "operational"), comp("c2", "Webhook", "operational")]
old_resolved = {"id": "old1", "name": "Old outage", "status": "resolved",
                "components": [{"name": "LIFF"}],
                "incident_updates": [upd("u0", "resolved", "2026-11-19T13:00:00Z", "This incident has been resolved.")]}

# 1. first run, all OK, only old resolved incident -> nothing sent
DATA["summary"] = {"status": {"description": "All Systems Operational"}, "components": ok_comps}
DATA["incidents"] = [old_resolved]
rc, msgs, p = run()
assert rc == 0 and msgs == [], (rc, msgs, p.stderr)

# 2. same data again -> nothing
rc, msgs, _ = run(); assert msgs == []

# 3. outage starts: new incident + component change
new_inc = {"id": "n1", "name": "Outage report in the Messaging API", "status": "investigating",
           "components": [{"name": "API"}],
           "incident_updates": [upd("a1", "investigating", "2026-11-26T09:00:00Z", "We are <b>investigating</b>.")]}
DATA["incidents"] = [new_inc, old_resolved]
DATA["summary"]["components"] = [comp("c1", "API", "major_outage"), comp("c2", "Webhook", "operational")]
rc, msgs, _ = run()
assert len(msgs) == 2, msgs
assert "Investigating" in msgs[0] and "16:00" in msgs[0] and "<b>" not in msgs[0], msgs[0]
assert "API" in msgs[1] and "Major Outage" in msgs[1]

# 4. rerun -> dedupe, nothing
rc, msgs, _ = run(); assert msgs == [], msgs

# 5. LINE push fails on resolve -> exit 1, retried next run
new_inc["status"] = "resolved"
new_inc["incident_updates"].append(upd("a2", "resolved", "2026-11-26T09:30:00Z", "Resolved."))
DATA["summary"]["components"] = ok_comps
DATA["line_fail"] = True
rc, msgs, _ = run(); assert rc == 1 and msgs == []
DATA["line_fail"] = False
rc, msgs, _ = run()
assert rc == 0 and len(msgs) == 2 and "Resolved" in msgs[0] and "Operational" in msgs[1], msgs
rc, msgs, _ = run(); assert msgs == []

# 6. RSS fallback when JSON API is unavailable (fresh state)
os.remove(env["STATE_FILE"])
DATA["json_ok"] = False; DATA["atom_ok"] = False
rss_item = lambda desc: f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Outage report in LIFF</title><description>{desc}</description>
<pubDate>Thu, 26 Nov 2026 18:00:00 +0900</pubDate><link>{base}/incidents/x1</link><guid>{base}/incidents/x1</guid></item>
</channel></rss>"""
DATA["rss"] = rss_item("&lt;p&gt;&lt;strong&gt;Investigating&lt;/strong&gt; - Looking into it.&lt;/p&gt;")
rc, msgs, _ = run()
assert len(msgs) == 1 and "Investigating" in msgs[0] and "Looking into it." in msgs[0], msgs
rc, msgs, _ = run(); assert msgs == []
DATA["rss"] = rss_item("&lt;p&gt;&lt;strong&gt;Resolved&lt;/strong&gt; - Fixed.&lt;/p&gt;&lt;p&gt;&lt;strong&gt;Investigating&lt;/strong&gt; - Looking into it.&lt;/p&gt;")
rc, msgs, _ = run(); assert len(msgs) == 1 and "✅" in msgs[0] and "Fixed." in msgs[0] and "Looking" not in msgs[0], msgs

# 6b. Atom feed (real sample from api.line-status.info) when JSON is unavailable
os.remove(env["STATE_FILE"])
DATA["atom_ok"] = True
real = open(os.path.join(HERE, "fixtures", "history.atom"), encoding="utf-8").read()
DATA["atom"] = real
rc, msgs, p = run(); assert rc == 0 and msgs == [], (msgs, p.stderr)   # all resolved -> seed silently
assert '"last_source": "atom"' in open(env["STATE_FILE"]).read()
# a new incident appears at the top of the feed
new_entry = """  <entry>
    <id>tag:api.line-status.info,2005:Incident/99999999</id>
    <published>2026-11-26T18:10:00+09:00</published>
    <updated>2026-11-26T18:10:00+09:00</updated>
    <link rel="alternate" type="text/html" href="https://api.line-status.info/incidents/newone"/>
    <title>Outage report in the Messaging API</title>
    <content type="html">&lt;p&gt; &lt;small&gt;Nov &lt;var data-var='date'&gt;26&lt;/var&gt;, &lt;var data-var='time'&gt;18:10&lt;/var&gt; JST&lt;/small&gt;&lt;br&gt; &lt;strong&gt;Investigating&lt;/strong&gt; - Some webhook events are delayed. &lt;/p&gt;</content>
  </entry>
"""
DATA["atom"] = real.replace("  <entry>", new_entry + "  <entry>", 1)
rc, msgs, _ = run()
assert len(msgs) == 1 and "Investigating" in msgs[0] and "16:10" in msgs[0] and "webhook events are delayed" in msgs[0] and "incidents/newone" in msgs[0], msgs
rc, msgs, _ = run(); assert msgs == []
# switching to RSS for the same incident must not re-send
DATA["atom_ok"] = False
DATA["rss"] = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Outage report in the Messaging API</title><description>&lt;p&gt; &lt;small&gt;Nov &lt;var data-var='date'&gt;26&lt;/var&gt;, &lt;var data-var='time'&gt;18:10&lt;/var&gt; JST&lt;/small&gt;&lt;br&gt; &lt;strong&gt;Investigating&lt;/strong&gt; - Some webhook events are delayed. &lt;/p&gt;</description>
<link>https://api.line-status.info/incidents/newone</link><guid>https://api.line-status.info/incidents/newone</guid></item></channel></rss>"""
rc, msgs, _ = run(); assert msgs == [], msgs
DATA["atom_ok"] = True

# 7. Google Chat + LINE together; LINE down must not re-spam Google Chat
os.remove(env["STATE_FILE"])
DATA["json_ok"] = True
env["GOOGLE_CHAT_WEBHOOK_URL"] = base + "/gchat?key=SECRETKEY&token=SECRETTOKEN"
DATA["incidents"] = [old_resolved]; DATA["summary"]["components"] = ok_comps
rc, msgs, _ = run(); assert msgs == [] and GCHAT == []
inc2 = {"id": "n2", "name": "Outage report in the Messaging API", "status": "investigating",
        "components": [{"name": "API"}],
        "incident_updates": [upd("b1", "investigating", "2026-11-26T10:00:00Z", "Investigating.")]}
DATA["incidents"] = [inc2, old_resolved]
DATA["line_fail"] = True
rc, msgs, p = run()
assert rc == 1 and msgs == [] and len(GCHAT) == 1 and "Investigating" in GCHAT[0], (GCHAT, p.stderr)
assert "SECRET" not in p.stderr + p.stdout
rc, msgs, _ = run(); assert rc == 1 and GCHAT == [], GCHAT      # not re-sent to Google Chat
DATA["line_fail"] = False
rc, msgs, _ = run(); assert rc == 0 and len(msgs) == 1 and GCHAT == [], (msgs, GCHAT)
rc, msgs, _ = run(); assert msgs == [] and GCHAT == []
# Google Chat only (no LINE token)
os.remove(env["STATE_FILE"]); env.pop("LINE_CHANNEL_ACCESS_TOKEN")
DATA["incidents"] = [old_resolved]
rc, msgs, _ = run(); assert rc == 0
DATA["incidents"] = [inc2, old_resolved]
rc, msgs, _ = run(); assert rc == 0 and msgs == [] and len(GCHAT) == 1

print("ALL TESTS PASSED")

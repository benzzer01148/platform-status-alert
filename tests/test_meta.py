"""Test meta_monitor with fake metastatus.com RSS feeds + fake LINE / Google Chat (no network).
Run:  python -I tests/test_meta.py
"""
import json, os, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "meta_monitor.py")
FEED = {"messenger": [], "ig-messenger": []}
STATE = {"line_fail": False}
PUSHED, GCHAT = [], []

# channel header copied from the real feeds (empty = no outage)
HEAD = {"messenger": ("Messenger Platform Status", "Status updates for Messenger Platform"),
        "ig-messenger": ("Messenger API for Instagram Status", "Status updates for Messenger API for Instagram")}


def rss(slug):
    t, d = HEAD[slug]
    items = "".join(f"<item><title>{i['title']}</title><description>{i['desc']}</description>"
                    f"<pubDate>{i['pub']}</pubDate><guid>{i['guid']}</guid></item>" for i in FEED[slug])
    return f"""<rss xmlns:atom="http://www.w3.org/2005/Atom" version="2.0"><channel><title>{t}</title>
<link>https://metastatus.com/{slug}</link>
<atom:link href="https://metastatus.com/outage-events-feed-{slug}.rss" rel="self" type="application/rss+xml"/>
<description>{d}</description><lastBuildDate>Sat, 13 Jun 2026 17:31:55 GMT</lastBuildDate>{items}</channel></rss>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        for slug in FEED:
            if self.path == f"/outage-events-feed-{slug}.rss":
                self.send_response(200); self.end_headers(); self.wfile.write(rss(slug).encode()); return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.startswith("/gchat"):
            GCHAT.append(b["text"])
        else:
            if STATE["line_fail"]:
                self.send_response(500); self.end_headers(); return
            PUSHED.append(b["messages"][0]["text"])
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")


srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"; tmp = tempfile.mkdtemp()
env = dict(os.environ, META_BASE_URL=base, LINE_API_BASE=base, LINE_CHANNEL_ACCESS_TOKEN="t", LINE_TO="U1",
           GOOGLE_CHAT_WEBHOOK_URL=base + "/gchat?key=k&token=t", META_STATE_FILE=os.path.join(tmp, "s.json"))


def run():
    PUSHED.clear(); GCHAT.clear()
    p = subprocess.run([sys.executable, "-I", SCRIPT], env=env, capture_output=True, text=True)
    return p.returncode, list(PUSHED), p


# 1. real empty feeds -> nothing
rc, m, p = run(); assert rc == 0 and m == [], (m, p.stdout, p.stderr)
rc, m, _ = run(); assert m == []
# 2. outage appears on IG
FEED["ig-messenger"] = [{"title": "Messages delayed", "desc": "Some users may see delays sending messages.",
                         "pub": "Thu, 26 Nov 2026 09:00:00 GMT", "guid": "ev1"}]
rc, m, p = run()
assert len(m) == 1 and "Messenger API for Instagram" in m[0] and "Messages delayed" in m[0] and "16:00" in m[0], (m, p.stderr)
assert GCHAT == m
rc, m, _ = run(); assert m == [] and GCHAT == []
# 3. update to the same event; LINE down -> Google Chat once, LINE later
FEED["ig-messenger"][0]["desc"] = "Fix in progress."
STATE["line_fail"] = True
rc, m, _ = run(); assert rc == 1 and m == [] and len(GCHAT) == 1
rc, m, _ = run(); assert rc == 1 and GCHAT == []
STATE["line_fail"] = False
rc, m, _ = run(); assert rc == 0 and len(m) == 1 and "Fix in progress" in m[0] and GCHAT == []
# 4. event disappears -> back to normal
FEED["ig-messenger"] = []
rc, m, _ = run(); assert len(m) == 1 and "กลับมาปกติ" in m[0] and "Messages delayed" in m[0], m
rc, m, _ = run(); assert m == []
# 5. fresh state with an old event -> silent
os.remove(env["META_STATE_FILE"])
FEED["messenger"] = [{"title": "Old", "desc": "x", "pub": "Mon, 01 Jun 2026 00:00:00 GMT", "guid": "old"}]
rc, m, _ = run(); assert m == []
FEED["messenger"] = []
rc, m, _ = run(); assert m == []   # silent item dropped silently
print("META TESTS PASSED")

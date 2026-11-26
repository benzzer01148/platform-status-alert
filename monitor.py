#!/usr/bin/env python3
"""
LINE API Status -> LINE Messaging API / Google Chat alert.

Polls https://api.line-status.info (Atlassian Statuspage) and pushes a LINE
message when:
  * a new incident starts, or an incident gets a new update (Investigating,
    Identified, Monitoring, Resolved ...)
  * a component (Messaging API, Webhook, LINE Login, LIFF, ...) changes status
    (e.g. Operational -> Partial Outage, and back)

Dedupe: what has already been sent is kept in a JSON state file (STATE_FILE).
State is only saved after LINE accepts the push, so if sending fails (for
example because the Messaging API itself is down) the alert is retried on the
next run.

Standard library only. Configuration through environment variables:
  LINE_CHANNEL_ACCESS_TOKEN  LINE channel token (optional if Google Chat is set)
  GOOGLE_CHAT_WEBHOOK_URL    (optional) Google Chat space incoming webhook URL.
                             Alerts go to every channel that is configured.
  LINE_TO                    user/group/room ID to push to. Comma-separated for
                             several. If empty, the message is broadcast to every
                             friend of the bot (uses broadcast quota).
  STATUS_BASE_URL            default https://api.line-status.info
  STATE_FILE                 default state.json
  STATUS_SOURCE              auto (default: JSON, then Atom, then RSS) | json | atom | rss
  DRY_RUN=1                  print messages instead of sending
  NOTIFY_ON_FIRST_RUN=1      on the very first run, also send incidents that are
                             still open (default 1). Old resolved ones are never sent.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

BASE = os.environ.get("STATUS_BASE_URL", "https://api.line-status.info").rstrip("/")
LINE_API = os.environ.get("LINE_API_BASE", "https://api.line.me").rstrip("/")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
NOTIFY_ON_FIRST_RUN = os.environ.get("NOTIFY_ON_FIRST_RUN", "1") == "1"
TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
TARGETS = [t.strip() for t in os.environ.get("LINE_TO", "").split(",") if t.strip()]
GCHAT_URL = os.environ.get("GOOGLE_CHAT_WEBHOOK_URL", "").strip()
UA = "line-status-alert/1.0"
BKK = timezone(timedelta(hours=7))

STATUS_TH = {
    "operational": "✅ ปกติ (Operational)",
    "degraded_performance": "🟡 ช้าลง (Degraded Performance)",
    "partial_outage": "🟠 ใช้งานไม่ได้บางส่วน (Partial Outage)",
    "major_outage": "🔴 ล่ม (Major Outage)",
    "under_maintenance": "🔧 ปิดปรับปรุง (Maintenance)",
}
INCIDENT_TH = {
    "investigating": "🔴 กำลังตรวจสอบ (Investigating)",
    "identified": "🟠 พบสาเหตุแล้ว (Identified)",
    "monitoring": "🟡 กำลังเฝ้าดู (Monitoring)",
    "resolved": "✅ แก้ไขแล้ว (Resolved)",
    "postmortem": "📝 Postmortem",
    "scheduled": "🗓 นัดปิดปรับปรุง (Scheduled)",
    "in_progress": "🔧 กำลังปิดปรับปรุง (In progress)",
    "verifying": "🟡 กำลังตรวจสอบ (Verifying)",
    "completed": "✅ ปิดปรับปรุงเสร็จ (Completed)",
}


# ---------------------------------------------------------------- helpers
def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(BKK).strftime("%d/%m/%Y %H:%M") + " (เวลาไทย)"
    except ValueError:
        return iso


def strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- LINE push
def push_line(text: str) -> None:
    text = text[:4900]  # LINE text limit is 5000 chars
    msg = {"messages": [{"type": "text", "text": text}]}
    calls = []
    if TARGETS:
        for to in TARGETS:
            calls.append((f"{LINE_API}/v2/bot/message/push", {"to": to, **msg}))
    else:
        calls.append((f"{LINE_API}/v2/bot/message/broadcast", msg))
    for url, body in calls:
        # Same retry key for this message => LINE will not deliver it twice
        retry_key = str(uuid.uuid5(uuid.NAMESPACE_URL, url + json.dumps(body, sort_keys=True)))
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TOKEN}",
                "X-Line-Retry-Key": retry_key,
                "User-Agent": UA,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
        except urllib.error.HTTPError as e:
            # 409 = same retry key already accepted -> treat as sent
            if e.code == 409:
                continue
            raise RuntimeError(f"LINE API {e.code}: {e.read().decode(errors='replace')}") from e


# ---------------------------------------------------------------- Google Chat
def push_gchat(text: str) -> None:
    req = urllib.request.Request(
        GCHAT_URL,
        data=json.dumps({"text": text[:4000]}).encode(),
        method="POST",
        headers={"Content-Type": "application/json; charset=UTF-8", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except urllib.error.HTTPError as e:
        # never print the webhook URL (it contains the secret key/token)
        raise RuntimeError(f"Google Chat webhook HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Google Chat webhook unreachable: {e.reason}") from None


def channels() -> dict:
    ch = {}
    if TOKEN:
        ch["line"] = push_line
    if GCHAT_URL:
        ch["gchat"] = push_gchat
    return ch


def deliver(state: dict, key: str, text: str) -> bool:
    """Send text to every configured channel that hasn't received `key` yet.
    Returns True when all channels have it. Tracking per channel means that if
    LINE is down, Google Chat is not re-sent every run (and vice versa)."""
    if DRY_RUN:
        print("---- DRY RUN message ----\n" + text + "\n-------------------------")
        return True
    ch = channels()
    if not ch:
        raise RuntimeError("No channel configured: set LINE_CHANNEL_ACCESS_TOKEN and/or GOOGLE_CHAT_WEBHOOK_URL")
    pending = state.setdefault("pending", {})
    done = set(pending.get(key, []))
    ok = True
    for name, fn in ch.items():
        if name in done:
            continue
        try:
            fn(text)
            done.add(name)
        except Exception as e:
            ok = False
            print(f"[{name}] send failed, will retry next run: {e}", file=sys.stderr)
    if ok:
        pending.pop(key, None)
    else:
        pending[key] = sorted(done)
    return ok


# ---------------------------------------------------------------- JSON path
def fetch_json(path: str) -> dict:
    return json.loads(http_get(f"{BASE}{path}"))


def incident_message(inc: dict, upd: dict) -> str:
    comps = ", ".join(c.get("name", "") for c in inc.get("components") or []) or "-"
    status = INCIDENT_TH.get(upd.get("status", ""), upd.get("status", ""))
    body = strip_html(upd.get("body", ""))
    link = inc.get("shortlink") or f"{BASE}/incidents/{inc.get('id')}"
    return (
        f"[LINE API Status] {status}\n"
        f"{inc.get('name', '')}\n"
        f"บริการ: {comps}\n"
        f"เวลา: {fmt_time(upd.get('created_at') or upd.get('display_at'))}\n\n"
        f"{body}\n\n{link}"
    )


def check_json(state: dict, first_run: bool) -> list[tuple]:
    """Return list of (key, text, commit_fn). commit_fn marks it sent in state."""
    out = []
    summary = fetch_json("/api/v2/summary.json")
    incidents = fetch_json("/api/v2/incidents.json").get("incidents", [])

    # --- incident updates
    sent_updates = state.setdefault("sent_updates", {})
    for inc in sorted(incidents, key=lambda i: i.get("created_at") or ""):
        iid = inc["id"]
        done = set(sent_updates.get(iid, []))
        ups = sorted(inc.get("incident_updates") or [], key=lambda u: u.get("created_at") or "")
        new = [u for u in ups if u["id"] not in done]
        if not new:
            continue
        is_open = inc.get("status") not in ("resolved", "postmortem", "completed")
        if first_run and not (is_open and NOTIFY_ON_FIRST_RUN):
            sent_updates[iid] = [u["id"] for u in ups]  # seed silently
            continue
        if first_run:
            new = new[-1:]  # only latest update of an open incident
            sent_updates[iid] = [u["id"] for u in ups[:-1]]

        for u in new:
            def commit(iid=iid, uid=u["id"]):
                sent_updates.setdefault(iid, [])
                if uid not in sent_updates[iid]:
                    sent_updates[iid].append(uid)
            out.append((f"inc:{iid}:{u['id']}", incident_message(inc, u), commit))

    # --- component status changes
    comp_state = state.setdefault("components", {})
    changes = []
    for c in summary.get("components", []):
        if c.get("group"):  # group header only
            continue
        cid, new_status = c["id"], c.get("status", "")
        old = comp_state.get(cid)
        if old is None:
            if new_status != "operational" and (NOTIFY_ON_FIRST_RUN or not first_run):
                changes.append((cid, c.get("name", ""), None, new_status))
            else:
                comp_state[cid] = new_status
            continue
        if old != new_status:
            changes.append((cid, c.get("name", ""), old, new_status))
    if changes:
        lines = ["[LINE API Status] สถานะบริการเปลี่ยน"]
        for _, name, old, new in changes:
            frm = STATUS_TH.get(old, old) if old else "-"
            lines.append(f"• {name}: {frm} → {STATUS_TH.get(new, new)}")
        overall = (summary.get("status") or {}).get("description", "")
        if overall:
            lines.append(f"\nภาพรวม: {overall}")
        lines.append(BASE)

        def commit_c(changes=changes):
            for cid, _, _, new in changes:
                comp_state[cid] = new
        text = "\n".join(lines)
        out.append(("comp:" + hashlib.sha256(text.encode()).hexdigest()[:16], text, commit_c))
    return out


# ---------------------------------------------------------------- Atom / RSS feeds
ATOM_NS = "{http://www.w3.org/2005/Atom}"
JST = timezone(timedelta(hours=9))
FEED_STATUS = {
    "investigating": "🔴 กำลังตรวจสอบ (Investigating)",
    "identified": "🟠 พบสาเหตุแล้ว (Identified)",
    "update": "🔴 อัปเดต (Update)",
    "monitoring": "🟡 กำลังเฝ้าดู (Monitoring)",
    "resolved": "✅ แก้ไขแล้ว (Resolved)",
}


def parse_feed_updates(content_html: str) -> list[dict]:
    """Statuspage feed content = one <p> per update, newest first:
    <p><small>Nov <var>26</var>, <var>16:54</var> JST</small><br><strong>Resolved</strong> - body</p>"""
    out = []
    for block in re.findall(r"<p>(.*?)</p>", content_html, flags=re.S | re.I):
        st = re.search(r"<strong>(.*?)</strong>", block, flags=re.S)
        small = re.search(r"<small>(.*?)</small>", block, flags=re.S)
        body = block.split("</strong>", 1)[1] if "</strong>" in block else block
        body = strip_html(re.sub(r"^\s*-\s*", "", body))
        body = re.sub(r"^-\s*", "", body).strip()
        out.append({
            "status": strip_html(st.group(1)) if st else "",
            "when": re.sub(r"\s+", " ", strip_html(small.group(1))) if small else "",
            "body": body,
        })
    if not out:
        out.append({"status": "", "when": "", "body": strip_html(content_html)})
    return out


def jst_to_th(when: str) -> str:
    """'Nov 26, 16:54 JST' -> '26/11 14:54 (เวลาไทย)' (year is not in the feed)."""
    m = re.match(r"([A-Za-z]{3}) (\d{1,2}), (\d{1,2}):(\d{2}) JST", when)
    if not m:
        return when
    mon = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"].index(m.group(1).lower()) + 1
    y = datetime.now(BKK).year
    dt = datetime(y, mon, int(m.group(2)), int(m.group(3)), int(m.group(4)), tzinfo=JST)
    if dt > datetime.now(JST) + timedelta(days=2):  # Dec entry read in Jan
        dt = dt.replace(year=y - 1)
    return dt.astimezone(BKK).strftime("%d/%m/%Y %H:%M") + " (เวลาไทย)"


def read_feed(kind: str) -> list[dict]:
    """Return entries newest first: {link, title, content}. kind = atom|rss."""
    if kind == "atom":
        root = ET.fromstring(http_get(f"{BASE}/history.atom"))
        entries = []
        for e in root.findall(f"{ATOM_NS}entry"):
            link = e.find(f"{ATOM_NS}link")
            href = link.get("href") if link is not None else ""
            entries.append({"link": (href or e.findtext(f"{ATOM_NS}id") or "").strip(),
                            "title": e.findtext(f"{ATOM_NS}title") or "",
                            "content": e.findtext(f"{ATOM_NS}content") or ""})
        return entries
    root = ET.fromstring(http_get(f"{BASE}/history.rss"))
    return [{"link": (it.findtext("guid") or it.findtext("link") or "").strip(),
             "title": it.findtext("title") or "",
             "content": it.findtext("description") or ""} for it in root.findall("./channel/item")]


def check_feed(state: dict, first_run: bool, kind: str) -> list[tuple]:
    """Alert on the newest update of each incident. Dedupe key = incident URL +
    newest update (same for Atom and RSS, so switching source never re-sends)."""
    out = []
    seen = state.setdefault("feed_seen", {})
    for e in reversed(read_feed(kind)):  # oldest first
        ups = parse_feed_updates(e["content"])
        latest = ups[0]
        digest = hashlib.sha256(json.dumps(latest, sort_keys=True).encode()).hexdigest()[:16]
        key = e["link"]
        if seen.get(key) == digest:
            continue
        resolved = latest["status"].lower() == "resolved"
        if first_run and (resolved or not NOTIFY_ON_FIRST_RUN):
            seen[key] = digest
            continue
        status = FEED_STATUS.get(latest["status"].lower(), latest["status"] or "อัปเดต")
        text = (
            f"[LINE API Status] {status}\n"
            f"{e['title']}\n"
            f"เวลา: {jst_to_th(latest['when'])}\n\n"
            f"{latest['body'][:1500]}\n\n{e['link']}"
        )

        def commit(key=key, digest=digest):
            seen[key] = digest
        out.append((f"feed:{key}:{digest}", text, commit))
    return out


# ---------------------------------------------------------------- main
def send_test() -> int:
    """TEST_MESSAGE=1: send the newest incident + current overall status to every
    channel, without touching state (no dedupe)."""
    try:
        summary = fetch_json("/api/v2/summary.json")
        inc = fetch_json("/api/v2/incidents.json")["incidents"][0]
        upd = sorted(inc.get("incident_updates") or [], key=lambda u: u.get("created_at") or "")[-1]
        latest = incident_message(inc, upd)
        overall = (summary.get("status") or {}).get("description", "")
        comps = "\n".join(f"• {c['name']}: {STATUS_TH.get(c.get('status'), c.get('status'))}"
                          for c in summary.get("components", []) if not c.get("group"))
    except Exception as e:
        print(f"JSON failed ({e}); using Atom", file=sys.stderr)
        e0 = read_feed("atom")[0]
        u = parse_feed_updates(e0["content"])[0]
        latest = (f"[LINE API Status] {FEED_STATUS.get(u['status'].lower(), u['status'])}\n{e0['title']}\n"
                  f"เวลา: {jst_to_th(u['when'])}\n\n{u['body']}\n\n{e0['link']}")
        overall, comps = "", ""
    text = "🧪 [ข้อความทดสอบ] ระบบแจ้งเตือนทำงานปกติ\n"
    if overall:
        text += f"\nสถานะตอนนี้: {overall}\n{comps}\n"
    text += "\n— incident ล่าสุด —\n" + latest
    if DRY_RUN:
        print(text)
        return 0
    ch = channels()
    if not ch:
        print("No channel configured (set GOOGLE_CHAT_WEBHOOK_URL and/or LINE_CHANNEL_ACCESS_TOKEN)", file=sys.stderr)
        return 1
    rc = 0
    for name, fn in ch.items():
        try:
            fn(text)
            print(f"[{name}] test message sent")
        except Exception as e:
            rc = 1
            print(f"[{name}] test message failed: {e}", file=sys.stderr)
    return rc


def main() -> int:
    if os.environ.get("TEST_MESSAGE") == "1":
        return send_test()
    state = load_state()
    first_run = not state.get("initialized")
    mode = os.environ.get("STATUS_SOURCE", "auto").lower()
    order = {"json": ["json"], "atom": ["atom"], "rss": ["rss"]}.get(mode, ["json", "atom", "rss"])
    alerts, source = None, None
    for src in order:
        try:
            alerts = check_json(state, first_run) if src == "json" else check_feed(state, first_run, src)
            source = src
            break
        except Exception as e:
            print(f"{src} source failed ({e})", file=sys.stderr)
    if alerts is None:
        print("All status sources failed", file=sys.stderr)
        save_state(state)
        return 1

    failed = False
    for key, text, commit in alerts:
        if deliver(state, key, text):
            commit()
        else:
            failed = True
    state["initialized"] = True
    state["last_source"] = source
    save_state(state)
    print(f"source={source} alerts={len(alerts)} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

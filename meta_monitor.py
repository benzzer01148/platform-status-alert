#!/usr/bin/env python3
"""
Meta status (metastatus.com) -> LINE / Google Chat alert.

Reads Meta's per-product outage RSS feeds:
  https://metastatus.com/outage-events-feed-messenger.rss     (Messenger Platform)
  https://metastatus.com/outage-events-feed-ig-messenger.rss  (Messenger API for Instagram)

When there is no outage the feed has no <item>. GUESS: the item format has not
been seen yet (both feeds were empty when this was written), so items are
handled generically (title / description / pubDate / guid / link).

Alert rules (per feed):
  * a new item, or an item whose title/description changed  -> alert
  * an item we alerted on disappears from the feed          -> "back to normal" alert
First run: items already marked resolved, or older than 24 h, are remembered silently.

Env: same LINE_* / GOOGLE_CHAT_WEBHOOK_URL / DRY_RUN vars as monitor.py, plus
  META_FEEDS       default "messenger,ig-messenger"
  META_BASE_URL    default https://metastatus.com
  META_STATE_FILE  default meta_state.json
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor  # noqa: E402  reuse deliver(), http_get(), strip_html()

BASE = os.environ.get("META_BASE_URL", "https://metastatus.com").rstrip("/")
FEEDS = [p.strip() for p in os.environ.get("META_FEEDS", "messenger,ig-messenger").split(",") if p.strip()]
STATE_FILE = os.environ.get("META_STATE_FILE", "meta_state.json")
NAMES = {"messenger": "Messenger Platform", "ig-messenger": "Messenger API for Instagram"}


def fmt_date(pub: str) -> str:
    try:
        return email.utils.parsedate_to_datetime(pub).astimezone(monitor.BKK).strftime("%d/%m/%Y %H:%M น.")
    except Exception:
        return pub or ""


def is_old(pub: str) -> bool:
    try:
        return datetime.now(timezone.utc) - email.utils.parsedate_to_datetime(pub) > timedelta(hours=24)
    except Exception:
        return False


def main() -> int:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    rc = 0
    for slug in FEEDS:
        name = NAMES.get(slug, slug)
        page = f"{BASE}/{slug}"
        try:
            root = ET.fromstring(monitor.http_get(f"{BASE}/outage-events-feed-{slug}.rss"))
        except Exception as e:
            print(f"[{slug}] feed failed: {e}", file=sys.stderr)
            rc = 1
            continue
        if root.find("channel") is None:
            print(f"[{slug}] not an RSS feed; state unchanged", file=sys.stderr)
            rc = 1
            continue
        fs = state.setdefault(slug, {"active": {}})
        first = not fs.get("initialized")
        active = fs.setdefault("active", {})  # key -> {"digest", "title"}
        current = {}
        for it in root.findall("./channel/item"):
            title = (it.findtext("title") or "").strip()
            desc = monitor.strip_html(it.findtext("description") or "")
            pub = it.findtext("pubDate") or ""
            key = (it.findtext("guid") or it.findtext("link") or f"{title}|{pub}").strip()
            digest = hashlib.sha256(f"{title}\n{desc}".encode()).hexdigest()[:16]
            current[key] = True
            if active.get(key, {}).get("digest") == digest:
                continue
            resolved = "resolved" in f"{title} {desc}".lower()
            if first and (resolved or is_old(pub) or not monitor.NOTIFY_ON_FIRST_RUN):
                active[key] = {"digest": digest, "title": title, "silent": True}
                continue
            text = monitor.card(
                f"✅ {name} กลับมาใช้งานได้แล้ว" if resolved else f"🚨 {name} มีปัญหา",
                title, [("🕒 เวลา:", fmt_date(pub))], desc, it.findtext("link") or page)
            if monitor.deliver(fs, f"item:{key}:{digest}", text):
                active[key] = {"digest": digest, "title": title}
            else:
                rc = 1
        # items gone from the feed -> outage over (GUESS: Meta drops finished events)
        for key in [k for k in active if k not in current]:
            info = active[key]
            if info.get("silent"):
                del active[key]
                continue
            text = monitor.card(f"✅ {name} กลับมาใช้งานได้แล้ว", info.get("title", ""),
                                [("📍 สถานะ:", "ไม่มีรายการปัญหาบนหน้า status แล้ว")], link=page)
            if monitor.deliver(fs, f"gone:{key}", text):
                del active[key]
            else:
                rc = 1
        fs["initialized"] = True
        print(f"[{slug}] items={len(current)} tracked={len(active)}")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

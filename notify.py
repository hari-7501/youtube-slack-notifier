#!/usr/bin/env python3
"""Poll YouTube RSS feeds and post new long-form uploads to Slack.

Stdlib only, so the workflow needs no dependency install step.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHANNELS_FILE = ROOT / "channels.json"
STATE_FILE = ROOT / "state.json"

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
SHORTS_URL = "https://www.youtube.com/shorts/{}"
WATCH_URL = "https://youtu.be/{}"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

UA = "youtube-slack-notifier (+https://github.com/hari-7501/youtube-slack-notifier)"
TIMEOUT = 20
KEEP_PER_CHANNEL = 50


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_no_redirect = urllib.request.build_opener(_NoRedirect)


def log(msg):
    print(msg, flush=True)


def fetch_feed(channel_id):
    """Return feed entries, newest first, as dicts."""
    req = urllib.request.Request(FEED_URL.format(channel_id), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        root = ET.fromstring(resp.read())

    feed_author = root.findtext("atom:author/atom:name", default="", namespaces=NS)
    entries = []
    for entry in root.findall("atom:entry", NS):
        video_id = entry.findtext("yt:videoId", namespaces=NS)
        if not video_id:
            continue
        entries.append(
            {
                "id": video_id,
                "title": (entry.findtext("atom:title", default="", namespaces=NS) or "").strip(),
                "published": entry.findtext("atom:published", default="", namespaces=NS),
                "author": entry.findtext("atom:author/atom:name", namespaces=NS) or feed_author,
            }
        )
    return entries


def is_short(video_id):
    """True if the video is a Short.

    /shorts/<id> answers 200 for a real Short and 303 (redirect to /watch) for
    long-form. HEAD is enough, so this costs no body download. On an unexpected
    error we fail open and treat it as long-form: a stray Short notification is
    a smaller harm than silently swallowing a real upload.
    """
    req = urllib.request.Request(
        SHORTS_URL.format(video_id), method="HEAD", headers={"User-Agent": UA}
    )
    try:
        with _no_redirect.open(req, timeout=TIMEOUT) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return False
        log(f"    ! shorts probe HTTP {exc.code}, assuming long-form")
        return False
    except Exception as exc:  # network/DNS/timeout
        log(f"    ! shorts probe failed ({exc}), assuming long-form")
        return False


def is_future(published):
    """True if the entry is dated in the future (scheduled premiere or stream)."""
    if not published:
        return False
    try:
        when = datetime.fromisoformat(published)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when > datetime.now(timezone.utc)


def slack_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_message(entry, label):
    who = slack_escape(label or entry["author"] or "Unknown channel")
    title = slack_escape(entry["title"])
    return f"\U0001f3a5 *{who}* uploaded — {title}\n{WATCH_URL.format(entry['id'])}"


def post_slack(webhook, text):
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return 200 <= resp.status < 300


def load_json(path, default):
    if not path.exists():
        return default
    text = path.read_text().strip()
    return json.loads(text) if text else default


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be posted; touch neither Slack nor state.json",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="mark everything currently in every feed as seen, posting nothing",
    )
    args = parser.parse_args()

    channels = load_json(CHANNELS_FILE, [])
    if not channels:
        log(f"no channels configured in {CHANNELS_FILE.name}")
        return 1

    webhook = os.environ.get("SLACK_WEBHOOK", "").strip()
    if not webhook and not (args.dry_run or args.seed):
        log("SLACK_WEBHOOK is not set")
        return 1

    state = load_json(STATE_FILE, {})
    posted = failed = suppressed = 0
    changed = False

    for channel in channels:
        cid, label = channel["channel_id"], channel.get("label", "")
        log(f"{label} ({cid})")

        try:
            entries = fetch_feed(cid)
        except Exception as exc:
            # Leave this channel's state untouched so nothing is lost; the other
            # channels still run.
            log(f"    ! feed fetch failed: {exc}")
            failed += 1
            continue

        record = state.setdefault(cid, {"label": label, "seen": []})
        record["label"] = label
        seen = record["seen"]
        cold_start = not seen

        if cold_start or args.seed:
            record["seen"] = [e["id"] for e in entries][:KEEP_PER_CHANNEL]
            changed = True
            reason = "seeding" if args.seed else "cold start"
            log(f"    {reason}: recorded {len(record['seen'])} videos as seen, posting nothing")
            continue

        known = set(seen)
        fresh = [e for e in entries if e["id"] not in known]
        if not fresh:
            log("    nothing new")
            continue

        for entry in reversed(fresh):  # oldest first
            if is_future(entry["published"]):
                # Not marked seen, so it gets reconsidered once it actually airs.
                log(f"    scheduled for {entry['published']}, skipping: {entry['title'][:60]}")
                continue

            if is_short(entry["id"]):
                seen.insert(0, entry["id"])
                changed = True
                suppressed += 1
                log(f"    short, suppressed: {entry['title'][:60]}")
                continue

            message = format_message(entry, label)
            if args.dry_run:
                log("    would post:")
                log("      " + message.replace("\n", "\n      "))
                continue

            try:
                post_slack(webhook, message)
            except Exception as exc:
                # Stays unseen and is retried next run. A duplicate is
                # preferable to a dropped notification.
                log(f"    ! slack post failed ({exc}); will retry next run")
                failed += 1
                continue

            seen.insert(0, entry["id"])
            changed = True
            posted += 1
            log(f"    posted: {entry['title'][:60]}")

        del seen[KEEP_PER_CHANNEL:]

    if changed and not args.dry_run:
        STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    log(f"done: {posted} posted, {suppressed} shorts suppressed, {failed} failures")
    if failed and not posted and failed >= len(channels):
        return 1  # everything broke; make the run red
    return 0


if __name__ == "__main__":
    sys.exit(main())

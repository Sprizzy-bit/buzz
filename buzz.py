#!/usr/bin/env python3
"""BUzz: nag your phone about eLearn deadlines until you tap Done.

Reads your eLearn (D2L Brightspace / Moodle / any LMS) calendar iCal feed,
finds upcoming assignments, and sends push alerts through ntfy that get
more frequent and louder as each deadline gets closer. Every alert has a
"Done" button; tap it and that assignment stops buzzing.

Standard library only. Run it every 5-10 minutes (GitHub Actions or cron).

Settings (environment variables):
  ELEARN_ICS_URL   required. Your eLearn calendar subscription link.
  NTFY_TOPIC       required. A long, unguessable topic name, e.g. buzz-ron-8f3k2q9x.
  NTFY_SERVER      default https://ntfy.sh
  TZ_NAME          your timezone, default America/Chicago. Used for all-day/floating dates.
  INCLUDE_REGEX    which calendar events count as work (case-insensitive).
  EXCLUDE_REGEX    events to ignore even if they match INCLUDE_REGEX.
  LOOKAHEAD_DAYS   how far ahead to start reminding, default 7.
  OVERDUE_HOURS    keep nagging this long after the deadline, default 24.
  QUIET_HOURS      e.g. "01-07": no alerts in that window unless due within 6 hours.
  STATE_FILE       default state.json next to this script.
  DRY_RUN=1        print what would be sent instead of sending.
  NOW=...          ISO time to pretend it is (for testing).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HERE = Path(__file__).resolve().parent

DEFAULT_INCLUDE = (
    r"due|assignment|quiz|exam|midterm|final|test|dropbox|submi|project|essay|"
    r"report|lab|homework|hw\b|discussion|deadline|presentation"
)

# (max hours left, minutes between alerts, ntfy priority, label)
TIERS = [
    (1, 10, 5, "DUE WITHIN THE HOUR"),
    (3, 15, 5, "Due in a few hours"),
    (6, 30, 5, "Due today"),
    (24, 180, 4, "Due within 24h"),
    (72, 480, 3, "Due in the next 3 days"),
    (None, 1440, 2, "Coming up"),  # anything else inside LOOKAHEAD_DAYS
]
OVERDUE_TIER = (30, 5, "OVERDUE")

WINDOWS_TZ = {
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns",
    "GMT Standard Time": "Europe/London",
    "AUS Eastern Standard Time": "Australia/Sydney",
}


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


# ---------------------------------------------------------------- iCal parsing

def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def unescape(v: str) -> str:
    return (v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\"))


def get_zone(name: str | None, fallback: ZoneInfo) -> ZoneInfo:
    if not name:
        return fallback
    name = name.strip('"')
    try:
        return ZoneInfo(WINDOWS_TZ.get(name, name))
    except (ZoneInfoNotFoundError, ValueError):
        return fallback


def parse_dt(value: str, params: dict, local: ZoneInfo) -> datetime | None:
    value = value.strip()
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            d = datetime.strptime(value[:8], "%Y%m%d")
            # all-day deadline: treat as end of that day
            return d.replace(hour=23, minute=59, tzinfo=local)
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        return dt.replace(tzinfo=get_zone(params.get("TZID"), local))
    except ValueError:
        return None


def parse_events(text: str, local: ZoneInfo) -> list[dict]:
    events, cur = [], None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            parts = head.split(";")
            key = parts[0].upper()
            params = {}
            for p in parts[1:]:
                if "=" in p:
                    k, v = p.split("=", 1)
                    params[k.upper()] = v
            if key in ("DTSTART", "DTEND", "DUE"):
                cur[key] = parse_dt(value, params, local)
            elif key in ("SUMMARY", "UID", "DESCRIPTION", "URL", "LOCATION", "CATEGORIES"):
                cur[key] = unescape(value)
    out = []
    for e in events:
        when = e.get("DUE") or e.get("DTEND") or e.get("DTSTART")
        if not when or not e.get("SUMMARY"):
            continue
        uid = e.get("UID") or f"{e['SUMMARY']}|{when.isoformat()}"
        # A rescheduled deadline should buzz again, so key on uid + time.
        key = f"{uid}@{when.astimezone(timezone.utc):%Y%m%dT%H%M}"
        out.append({"key": key, "title": e["SUMMARY"].strip(), "due": when,
                    "url": e.get("URL"), "course": e.get("LOCATION") or e.get("CATEGORIES"),
                    "desc": e.get("DESCRIPTION", "")})
    return out


# ---------------------------------------------------------------- ntfy

def http(method, url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    req.add_header("User-Agent", "buzz-deadline-nagger/1.0")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def read_control_messages(server, topic) -> list[dict]:
    """Done/snooze taps are POSTed to <topic>-done; read them back."""
    url = f"{server}/{topic}-done/json?poll=1&since=all"
    try:
        body = http("GET", url)
    except Exception as ex:  # network hiccup: just skip this run's sync
        print(f"warn: could not read done topic: {ex}", file=sys.stderr)
        return []
    msgs = []
    for line in body.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("event") == "message" and m.get("message"):
            msgs.append(m)
    return msgs


def send(server, topic, title, message, priority, tags, actions, click, dry):
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": tags, "actions": actions}
    if click:
        payload["click"] = click
    if dry:
        print(f"[DRY] p{priority} {title} :: {message.splitlines()[0]}")
        return
    http("POST", server, json.dumps(payload).encode(),
         {"Content-Type": "application/json"})


# ---------------------------------------------------------------- main logic

def fmt_left(delta: timedelta) -> str:
    s = int(abs(delta.total_seconds()))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def in_quiet(now_local: datetime, spec: str | None) -> bool:
    if not spec:
        return False
    try:
        a, b = (int(x) for x in spec.split("-"))
    except ValueError:
        return False
    h = now_local.hour
    return a <= h < b if a < b else (h >= a or h < b)


def pick_tier(hours_left: float):
    if hours_left < 0:
        return OVERDUE_TIER
    for max_h, every, prio, label in TIERS:
        if max_h is None or hours_left <= max_h:
            return every, prio, label
    return TIERS[-1][1:]


def main() -> int:
    ics_url = env("ELEARN_ICS_URL")
    topic = env("NTFY_TOPIC")
    if not ics_url or not topic:
        print("Set ELEARN_ICS_URL and NTFY_TOPIC (see README.md).", file=sys.stderr)
        return 2
    server = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    local = get_zone(env("TZ_NAME", "America/Chicago"), ZoneInfo("UTC"))
    include = re.compile(env("INCLUDE_REGEX", DEFAULT_INCLUDE), re.I)
    exclude_s = env("EXCLUDE_REGEX")
    exclude = re.compile(exclude_s, re.I) if exclude_s else None
    lookahead = timedelta(days=float(env("LOOKAHEAD_DAYS", "7")))
    overdue = timedelta(hours=float(env("OVERDUE_HOURS", "24")))
    dry = env("DRY_RUN") == "1"
    now = datetime.fromisoformat(env("NOW")) if env("NOW") else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=local)
    state_path = Path(env("STATE_FILE", str(HERE / "state.json")))

    state = {"sent": {}, "done": [], "snooze": {}}
    if state_path.exists():
        try:
            state.update(json.loads(state_path.read_text()))
        except json.JSONDecodeError:
            pass
    done = set(state["done"])
    newly_done: set[str] = set()
    newly_snoozed: dict[str, int] = {}

    # 1. sync Done / Snooze taps
    if not dry or env("SYNC_IN_DRY_RUN") == "1":
        for m in read_control_messages(server, topic):
            text = m["message"].strip()
            if text.startswith("done "):
                if text[5:] not in done:
                    newly_done.add(text[5:])
                done.add(text[5:])
            elif text.startswith("snooze "):
                _, mins, key = text.split(" ", 2)
                until = m.get("time", time.time()) + int(mins) * 60
                if until > state["snooze"].get(key, 0) and until > now.timestamp():
                    newly_snoozed[key] = int(mins)
                state["snooze"][key] = max(state["snooze"].get(key, 0), until)

    # 2. fetch calendar
    if ics_url.startswith("file://") or os.path.exists(ics_url):
        text = Path(ics_url.removeprefix("file://")).read_text()
    else:
        text = http("GET", ics_url.replace("webcal://", "https://", 1), timeout=60)
    events = parse_events(text, local)

    # confirm Done / Snooze taps so you know they registered
    titles = {e["key"]: e["title"] for e in events}
    for key in sorted(newly_done):
        if key in titles:
            try:
                send(server, topic, f"✅ Done: {titles[key]}",
                     "Got it. No more alerts for this one.", 2, ["white_check_mark"], [], None, dry)
            except Exception as ex:
                print(f"warn: confirm failed: {ex}", file=sys.stderr)
    for key, mins in newly_snoozed.items():
        if key in titles and key not in done:
            try:
                send(server, topic, f"😴 Snoozed: {titles[key]}",
                     f"Quiet for {mins} min, then it starts again.", 2, ["zzz"], [], None, dry)
            except Exception as ex:
                print(f"warn: confirm failed: {ex}", file=sys.stderr)

    # 3. decide who gets buzzed
    sent_count = 0
    live_keys = set()
    for ev in sorted(events, key=lambda e: e["due"]):
        left = ev["due"] - now
        if left > lookahead or left < -overdue:
            continue
        blob = f"{ev['title']} {ev['desc'][:200]}"
        if not include.search(blob) or (exclude and exclude.search(blob)):
            continue
        live_keys.add(ev["key"])
        if ev["key"] in done:
            continue
        if state["snooze"].get(ev["key"], 0) > now.timestamp():
            continue
        hours = left.total_seconds() / 3600
        every, prio, label = pick_tier(hours)
        if hours > 6 and in_quiet(now.astimezone(local), env("QUIET_HOURS")):
            continue
        last = state["sent"].get(ev["key"])
        # -30s slack so a 10-minute schedule doesn't skip every other run
        if last and now.timestamp() - last < every * 60 - 30:
            continue

        due_local = ev["due"].astimezone(local)
        when = f"{due_local:%a %b %d, %I:%M %p}".replace(" 0", " ")
        if hours >= 0:
            msg = f"{label}: {fmt_left(left)} left (due {when})"
        else:
            msg = f"Was due {fmt_left(left)} ago ({when}). Submit it late or mark it done."
        if ev["course"]:
            msg += f"\n{ev['course']}"
        tags = ["rotating_light"] if prio == 5 else ["warning"] if prio == 4 else ["books"]
        done_url = f"{server}/{topic}-done"
        actions = [
            {"action": "http", "label": "Done ✅", "url": done_url, "method": "POST",
             "body": f"done {ev['key']}", "clear": True,
             "headers": {"Title": f"✅ Done: {ev['title']}", "Priority": "2",
                         "Tags": "white_check_mark"}},
            {"action": "http", "label": "Snooze 1h", "url": done_url, "method": "POST",
             "body": f"snooze 60 {ev['key']}", "clear": True,
             "headers": {"Title": f"😴 Snoozed 1h: {ev['title']}", "Priority": "2",
                         "Tags": "zzz"}},
        ]
        if ev["url"]:
            actions.append({"action": "view", "label": "Open", "url": ev["url"]})
        title = f"{'🚨 ' if prio == 5 else ''}{ev['title']}"
        # final hour: triple-tap so it really buzzes
        burst = 3 if 0 <= hours < 1 else 1
        try:
            for i in range(burst):
                send(server, topic, title, msg, prio, tags, actions, ev["url"], dry)
                if burst > 1 and i < burst - 1 and not dry:
                    time.sleep(4)
        except Exception as ex:
            print(f"warn: send failed for {ev['title']}: {ex}", file=sys.stderr)
            continue
        state["sent"][ev["key"]] = now.timestamp()
        sent_count += 1

    # 4. tidy and save state (forget anything no longer in the window)
    state["sent"] = {k: v for k, v in state["sent"].items() if k in live_keys}
    state["snooze"] = {k: v for k, v in state["snooze"].items() if k in live_keys}
    state["done"] = sorted(k for k in done if k in live_keys)
    if not dry:
        state_path.write_text(json.dumps(state, indent=1))
    print(f"{len(live_keys)} assignment(s) in window, {len(state['done'])} done, "
          f"{sent_count} alert(s) sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

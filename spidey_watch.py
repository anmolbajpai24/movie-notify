#!/usr/bin/env python3
"""
spidey_watch.py — pings your phone the moment showtimes for a target date go live.

How it works: it replays the exact showtimes request the cinema site makes
(you capture it once from DevTools), pointed at the not-yet-live date.
When the response starts containing sessions, it sends a push via ntfy
(and optionally Telegram) and exits.

Runbook:
 1. Phone: install the ntfy app, subscribe to the topic you set in NTFY_TOPIC.
 2. On pvrinox.com (or district.in): pick the movie + city, open
    DevTools > Network > Fetch/XHR, then click a date that IS already live.
    Find the request whose response JSON contains the showtimes you see
    on screen. Right-click it > Copy > Copy as cURL.
 3. Convert that cURL into METHOD / URL / HEADERS / PAYLOAD below.
    (Fastest: paste the cURL into Claude Code and ask it to fill this file.)
    Keep headers exactly as captured — User-Agent, x-* app headers, cookies.
 4. Dry run with the LIVE date still in PAYLOAD:
       python3 spidey_watch.py --check     -> should say WOULD ALERT: yes
    Then set the date in PAYLOAD to the target date and:
       python3 spidey_watch.py --check     -> should say WOULD ALERT: no
    If the target date wrongly says yes, copy the real sessions path from
    the printed table into DETECT_PATH to pin detection to it.
 5. Arm it:  tmux new -s spidey 'python3 spidey_watch.py'
    Keep the laptop awake — WSL2 pauses when Windows sleeps.
 6. Laptop-free: run `python3 spidey_watch.py --once` from any scheduler
    (cron, GitHub Actions — see watch.yml). One stateless poll per run.
"""

import json
import random
import re
import sys
import time
from datetime import datetime

import requests

# ------------------------------------------------------------------ config --

NTFY_TOPIC = "spiderman-tuesday-1234"     # your ntfy.sh topic (make it unguessable)
BOOKING_URL = ("https://www.pvrcinemas.com/moviesessions/"
               "Delhi-NCR/SPIDERMAN-BRAND-NEW-DAY/35294")  # opened on notification tap

POLL_BASE_SECONDS = 600                   # ~10 min between checks
POLL_JITTER_SECONDS = 180                 # +/- random spread so polling looks human

# Optional: fill both to also get a Telegram message.
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

# Optional: pin detection to one JSON path (copy it from --check output),
# e.g. "output.cinemaSessions". Leave "" to use the generic heuristic.
DETECT_PATH = ""

# Optional: only alert when THIS cinema appears in the response (PVR schema:
# output.movieCinemaSessions[].cinema). theatreId is matched first, then a
# case-insensitive name substring. Leave both "" to watch the whole city.
# NB: id 310 = "INOX Pacific Mall, Jasola Delhi" (id 543 is a different
# "Pacific Mall" in Faridabad — do not match by the word "pacific").
TARGET_THEATRE_ID = "310"
TARGET_CINEMA_NAME = "jasola"

# Experience label counted and named in the alert text (info, not a gate) —
# the alert fires when the target cinema lists ANY shows for the date.
TARGET_EXPERIENCE = "SCREEN X"

# ---------------------------------------------- request (paste from DevTools) --

METHOD = "POST"
URL = "https://api3.pvrcinemas.com/api/v1/booking/content/msessions"

# Captured live from www.pvrcinemas.com on 2026-07-31 (Chrome DevTools-equivalent
# XHR hook). The site sends an EMPTY bearer when logged out — the endpoint is
# anonymous; no cookies were attached by the app to this API call.
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Authorization": "Bearer ",
    "chain": "PVR",
    "city": "Delhi-NCR",
    "appVersion": "1.0",
    "platform": "WEBSITE",
    "country": "INDIA",
    "Origin": "https://www.pvrcinemas.com",
    "Referer": "https://www.pvrcinemas.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
}

# The captured request body (or query params if it was a GET).
# Change ONLY the date field to the target date, e.g. "2026-08-04".
PAYLOAD = {
    "city": "Delhi-NCR",
    "mid": "35294",              # SPIDERMAN BRAND NEW DAY
    "experience": "ALL",
    "specialTag": "ALL",
    "lat": "28.632445",
    "lng": "77.2198104",
    "lang": "ALL",
    "format": "ALL",
    "dated": "2026-08-04",       # <- the date field
    "time": "08:00-24:00",
    "cinetype": "ALL",
    "hc": "ALL",
    "adFree": False,
    "bbt": False,
}

# ------------------------------------------------------------------- guts --

SHOWY_KEY = re.compile(r"session|show|time", re.I)


def log(msg: str) -> None:
    print(f"[{datetime.now():%d %b %H:%M:%S}] {msg}", flush=True)


def fetch() -> dict:
    kwargs = {"headers": HEADERS, "timeout": 30}
    if METHOD.upper() == "GET":
        kwargs["params"] = PAYLOAD
    else:
        kwargs["json"] = PAYLOAD
    r = requests.request(METHOD, URL, **kwargs)
    r.raise_for_status()
    return r.json()


def find_signals(node, path="", hits=None) -> dict:
    """Walk the JSON. Record every non-empty list whose key name smells like
    sessions/shows/times. Schema-agnostic on purpose — the --check dry run
    tells you whether it needs pinning via DETECT_PATH."""
    if hits is None:
        hits = {}
    if isinstance(node, dict):
        for k, v in node.items():
            find_signals(v, f"{path}.{k}" if path else k, hits)
    elif isinstance(node, list):
        leaf = path.rsplit(".", 1)[-1]
        if node and SHOWY_KEY.search(leaf):
            hits[path] = hits.get(path, 0) + len(node)
        for item in node:
            find_signals(item, path + "[]", hits)
    return hits


def would_alert(signals: dict) -> bool:
    if DETECT_PATH:
        return signals.get(DETECT_PATH, 0) > 0
    return sum(signals.values()) > 0


def narrow(data):
    """If a target cinema is configured, cut the response down to that
    cinema's block(s) — same shape as the original — so detection means
    'this theatre is listed for the date', not 'anything in the city'."""
    if not (TARGET_THEATRE_ID or TARGET_CINEMA_NAME):
        return data
    try:
        blocks = data["output"]["movieCinemaSessions"] or []
    except (KeyError, TypeError):
        blocks = []
    keep = []
    for blk in blocks:
        cin = blk.get("cinema") or {}
        cid = str(cin.get("theatreId", ""))
        name = str(cin.get("name", ""))
        if TARGET_THEATRE_ID and cid == TARGET_THEATRE_ID:
            keep.append(blk)
        elif TARGET_CINEMA_NAME and TARGET_CINEMA_NAME.lower() in name.lower():
            keep.append(blk)
    return {"output": {"movieCinemaSessions": keep}}


def experience_counts(data) -> dict:
    """{experience_label: n_shows} across the (narrowed) cinema blocks."""
    out = {}
    try:
        blocks = data["output"]["movieCinemaSessions"] or []
    except (KeyError, TypeError):
        return out
    for blk in blocks:
        for es in blk.get("experienceSessions") or []:
            label = (es.get("experience") or "").strip() or "DEFAULT"
            out[label] = out.get(label, 0) + len(es.get("shows") or [])
    return out


def alert_message(data) -> str:
    """Alert text: which cinema went live, how many shows, which experiences."""
    exps = experience_counts(data)
    total = sum(exps.values())
    try:
        where = data["output"]["movieCinemaSessions"][0]["cinema"]["name"]
    except (KeyError, IndexError, TypeError):
        where = "Target cinema"
    parts = ", ".join(f"{n}x {lbl}" for lbl, n in
                      sorted(exps.items(), key=lambda kv: -kv[1]))
    want = ""
    if TARGET_EXPERIENCE:
        norm = TARGET_EXPERIENCE.replace(" ", "").upper()
        n_want = sum(n for lbl, n in exps.items()
                     if lbl.replace(" ", "").upper() == norm)
        want = (f" {TARGET_EXPERIENCE}: {n_want} shows." if n_want
                else f" No {TARGET_EXPERIENCE} shows listed yet.")
    return (f"{where} just listed {total} shows for {PAYLOAD.get('dated')} "
            f"({parts}).{want} Go book before the good seats vanish.")


def notify(title: str, message: str, priority: str = "urgent") -> bool:
    ok = False
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode(),
            headers={"Title": title, "Priority": priority,
                     "Click": BOOKING_URL, "Tags": "rotating_light"},
            timeout=15,
        )
        ok = r.ok
    except requests.RequestException as e:
        log(f"ntfy send failed: {e}")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": f"{title}\n{message}"},
                timeout=15,
            )
            ok = ok or r.ok
        except requests.RequestException as e:
            log(f"telegram send failed: {e}")
    return ok


def sanity() -> None:
    if "PASTE" in URL or "CHANGE-ME" in NTFY_TOPIC:
        sys.exit("Fill in URL / HEADERS / PAYLOAD and NTFY_TOPIC first (see runbook).")


def check_once() -> None:
    sanity()
    raw = fetch()
    data = narrow(raw)
    signals = find_signals(data)
    log(f"top-level keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw)}")
    if TARGET_THEATRE_ID or TARGET_CINEMA_NAME:
        try:
            n_all = len(raw["output"]["movieCinemaSessions"] or [])
        except (KeyError, TypeError):
            n_all = 0
        n_kept = len(data["output"]["movieCinemaSessions"])
        log(f"cinemas in city response: {n_all}; matching target cinema: {n_kept}")
        log(f"experiences at target: {experience_counts(data) or '{}'}")
    if signals:
        log("candidate show lists found:")
        for p, n in sorted(signals.items(), key=lambda kv: -kv[1]):
            log(f"  {n:4d}  {p}")
    else:
        log("no session-like lists in response")
    log(f"WOULD ALERT: {'yes' if would_alert(signals) else 'no'}")


def watch() -> None:
    sanity()
    log(f"armed — polling every ~{POLL_BASE_SECONDS // 60} min")
    notify("Watcher armed", "Spidey showtime watcher is running. This is the test push.",
           priority="default")
    failures = 0
    while True:
        try:
            data = narrow(fetch())
            signals = find_signals(data)
            failures = 0
            if would_alert(signals):
                total = (signals.get(DETECT_PATH) if DETECT_PATH
                         else sum(signals.values()))
                log(f"SHOWS LIVE — {total} entries. Alerting and exiting.")
                notify("Spidey shows are LIVE", alert_message(data))
                return
            log("not live yet")
        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            failures += 1
            log(f"poll failed ({failures} in a row): {e}")
            if failures == 6:
                notify("Watcher is failing",
                       "6 consecutive poll failures — cookies/token may have expired. "
                       "Recapture the request from DevTools.", priority="default")
        sleep_for = POLL_BASE_SECONDS + random.randint(-POLL_JITTER_SECONDS,
                                                       POLL_JITTER_SECONDS)
        time.sleep(max(60, sleep_for))


def once() -> int:
    """Single stateless poll for cron / GitHub Actions (see watch.yml).
    Exit codes: 0 = not live yet, 42 = LIVE (alert sent), 1 = poll failed."""
    sanity()
    try:
        data = narrow(fetch())
    except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
        log(f"poll failed: {e}")
        return 1
    signals = find_signals(data)
    if would_alert(signals):
        total = (signals.get(DETECT_PATH) if DETECT_PATH
                 else sum(signals.values()))
        log(f"SHOWS LIVE — {total} entries. Alerting.")
        notify("Spidey shows are LIVE", alert_message(data))
        return 42
    log("not live yet")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        check_once()
    elif "--once" in sys.argv:
        sys.exit(once())
    else:
        watch()

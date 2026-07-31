#!/usr/bin/env python3
"""
spidey_watch.py — pings your phone the moment showtimes for a target date go live.

How it works: it replays the exact showtimes request the cinema site makes
(you capture it once from DevTools), pointed at the not-yet-live date.
When the target cinema lists TARGET_EXPERIENCE sessions (e.g. SCREEN X),
it sends an urgent push via ntfy (and optionally Telegram) and exits.
If the cinema goes live with only other formats, it sends a single
low-priority heads-up and keeps watching.

Runbook:
 1. Phone: install the ntfy app, subscribe to the topic you set in NTFY_TOPIC.
 2. On pvrinox.com (or district.in): pick the movie + city, open
    DevTools > Network > Fetch/XHR, then click a date that IS already live.
    Find the request whose response JSON contains the showtimes you see
    on screen. Right-click it > Copy > Copy as cURL.
 3. Convert that cURL into METHOD / URL / HEADERS / PAYLOAD below.
    (Fastest: paste the cURL into Claude Code and ask it to fill this file.)
    Keep headers exactly as captured — User-Agent, x-* app headers, cookies.
 4. Dry run with a LIVE date in PAYLOAD that lists TARGET_EXPERIENCE shows:
       python3 spidey_watch.py --check     -> should say WOULD ALERT: yes
    Then set the date in PAYLOAD to the target date and:
       python3 spidey_watch.py --check     -> should say WOULD ALERT: no
    If the target date wrongly says yes, copy the real sessions path from
    the printed table into DETECT_PATH to pin detection to it.
 5. Arm it:  tmux new -s spidey 'python3 spidey_watch.py'
    Keep the laptop awake — WSL2 pauses when Windows sleeps.
 6. Laptop-free: run `python3 spidey_watch.py --once` from any scheduler
    (cron, GitHub Actions — see watch.yml). One stateless poll per run.
 7. Test without pinging the main topic:
       python3 spidey_watch.py --check --date 2026-08-02
    --date overrides PAYLOAD's date for this run only and reroutes EVERY
    push (armed / urgent / soft / heartbeat) to NTFY_TOPIC + "-hb".
    --heartbeat adds a low-priority status ping per poll, always to the
    "-hb" topic. Subscribe to that second topic to use either flag.
"""

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ------------------------------------------------------------------ config --

NTFY_TOPIC = "spiderman-tuesday-1234"     # your ntfy.sh topic (make it unguessable)
BOOKING_URL = ("https://www.pvrcinemas.com/moviesessions/"
               "Delhi-NCR/SPIDERMAN-BRAND-NEW-DAY/35294")  # opened on notification tap

# Runtime flags — set from the CLI in __main__, never edited by hand.
# HEARTBEAT (--heartbeat): low-priority status ping after every poll, always
# to NTFY_TOPIC + "-hb". SAFE_MODE (--date): the run is a test — reroute
# EVERY push to the "-hb" topic so the main topic can never be pinged.
HEARTBEAT = False
SAFE_MODE = False

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

# Experience label that GATES the urgent alert: it fires only when the target
# cinema lists shows under this label for the date. If the cinema goes live
# with only other formats, a one-time low-priority heads-up is sent (tracked
# via LIVE_FLAG so restarts/cron runs never repeat it) and polling continues.
# Leave "" to alert on any show at the target cinema.
TARGET_EXPERIENCE = "SCREEN X"
LIVE_FLAG = Path(__file__).with_name(".spidey_live_no_screenx_sent")

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


def is_live(signals: dict) -> bool:
    """The (narrowed) response lists any shows at all — the cinema is live."""
    if DETECT_PATH:
        return signals.get(DETECT_PATH, 0) > 0
    return sum(signals.values()) > 0


def would_alert(signals: dict, data) -> bool:
    """Urgent-alert gate: TARGET_EXPERIENCE must have shows listed at the
    target cinema. Falls back to 'any shows' when TARGET_EXPERIENCE is ""."""
    if TARGET_EXPERIENCE:
        return screenx_count(data) > 0
    return is_live(signals)


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


def screenx_count(data) -> int:
    """Shows at the target cinema under the TARGET_EXPERIENCE label."""
    norm = TARGET_EXPERIENCE.replace(" ", "").upper()
    return sum(n for lbl, n in experience_counts(data).items()
               if lbl.replace(" ", "").upper() == norm)


def cinema_name(data) -> str:
    try:
        return data["output"]["movieCinemaSessions"][0]["cinema"]["name"]
    except (KeyError, IndexError, TypeError):
        return "Target cinema"


def alert_message(data) -> str:
    """Urgent alert text: the TARGET_EXPERIENCE count up front, then the
    full per-experience breakdown."""
    exps = experience_counts(data)
    total = sum(exps.values())
    parts = ", ".join(f"{n}x {lbl}" for lbl, n in
                      sorted(exps.items(), key=lambda kv: -kv[1]))
    if TARGET_EXPERIENCE:
        return (f"{cinema_name(data)} listed {screenx_count(data)}x "
                f"{TARGET_EXPERIENCE} for {PAYLOAD.get('dated')} "
                f"({total} shows total: {parts}). "
                f"Go book before the good seats vanish.")
    return (f"{cinema_name(data)} just listed {total} shows for "
            f"{PAYLOAD.get('dated')} ({parts}). "
            f"Go book before the good seats vanish.")


_soft_sent = False  # in-run dedupe for the heads-up below


def notify_live_no_screenx(data) -> None:
    """One-time low-priority heads-up: the cinema is live for the date but
    has no TARGET_EXPERIENCE shows yet. LIVE_FLAG (a file next to this
    script) makes it fire at most once across restarts and cron runs; it is
    only written after ntfy accepts the push, so a failed send retries.
    SAFE_MODE test runs ignore the file both ways — they dedupe in-memory
    only, and never write the flag the real run depends on."""
    global _soft_sent
    if _soft_sent or (not SAFE_MODE and LIVE_FLAG.exists()):
        return
    msg = (f"{cinema_name(data)} is live for {PAYLOAD.get('dated')}, "
           f"no {TARGET_EXPERIENCE} listed yet — watching on.")
    if notify(f"Live, waiting for {TARGET_EXPERIENCE}", msg, priority="low"):
        _soft_sent = True
        if not SAFE_MODE:
            LIVE_FLAG.touch()


def notify(title: str, message: str, priority: str = "urgent") -> bool:
    topic = NTFY_TOPIC + ("-hb" if SAFE_MODE else "")
    ok = False
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode(),
            headers={"Title": title, "Priority": priority,
                     "Click": BOOKING_URL, "Tags": "rotating_light"},
            timeout=15,
        )
        ok = r.ok
    except requests.RequestException as e:
        log(f"ntfy send failed: {e}")
    # Telegram has no side topic to divert to, so SAFE_MODE suppresses it.
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and not SAFE_MODE:
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


def heartbeat(n: int, status: str) -> None:
    """--heartbeat: LOW-priority status ping after every poll. Always posts
    to NTFY_TOPIC + "-hb" so the main topic stays alert-only."""
    if not HEARTBEAT:
        return
    where = TARGET_CINEMA_NAME.title() if TARGET_CINEMA_NAME \
        else PAYLOAD.get("city", "city")
    msg = (f"poll {n} {datetime.now():%H:%M} — "
           f"{where} {PAYLOAD.get('dated')}: {status}")
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}-hb",
            data=msg.encode(),
            headers={"Title": "Watcher heartbeat", "Priority": "low",
                     "Tags": "stopwatch"},
            timeout=15,
        )
    except requests.RequestException as e:
        log(f"heartbeat send failed: {e}")


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
    if TARGET_EXPERIENCE:
        log(f"{TARGET_EXPERIENCE} shows at target: {screenx_count(data)}")
    if signals:
        log("candidate show lists found:")
        for p, n in sorted(signals.items(), key=lambda kv: -kv[1]):
            log(f"  {n:4d}  {p}")
    else:
        log("no session-like lists in response")
    verdict = "yes" if would_alert(signals, data) else "no"
    log(f"WOULD ALERT: {verdict}")
    heartbeat(1, f"dry-run check — WOULD ALERT: {verdict}")


def watch() -> None:
    sanity()
    log(f"armed — polling every ~{POLL_BASE_SECONDS // 60} min")
    notify("Watcher armed", "Spidey showtime watcher is running. This is the test push.",
           priority="default")
    heartbeat(0, "watcher armed")
    failures = 0
    polls = 0
    while True:
        polls += 1
        try:
            data = narrow(fetch())
            signals = find_signals(data)
            failures = 0
            if would_alert(signals, data):
                n_sx = screenx_count(data) if TARGET_EXPERIENCE else \
                    sum(signals.values())
                log(f"LIVE — {n_sx}x {TARGET_EXPERIENCE or 'shows'}. "
                    f"Alerting and exiting.")
                notify(f"Spidey {TARGET_EXPERIENCE or 'shows'} LIVE",
                       alert_message(data))
                heartbeat(polls, f"{n_sx}x {TARGET_EXPERIENCE or 'shows'} "
                                 f"LIVE — alerted, exiting")
                return
            if TARGET_EXPERIENCE and is_live(signals):
                notify_live_no_screenx(data)
                log(f"live, but no {TARGET_EXPERIENCE} yet — watching on")
                heartbeat(polls, f"live, no {TARGET_EXPERIENCE} yet")
            else:
                log("not live yet")
                heartbeat(polls, "not live yet")
        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            failures += 1
            log(f"poll failed ({failures} in a row): {e}")
            heartbeat(polls, f"poll failed ({failures} in a row)")
            if failures == 6:
                notify("Watcher is failing",
                       "6 consecutive poll failures — cookies/token may have expired. "
                       "Recapture the request from DevTools.", priority="default")
        sleep_for = POLL_BASE_SECONDS + random.randint(-POLL_JITTER_SECONDS,
                                                       POLL_JITTER_SECONDS)
        time.sleep(max(60, sleep_for))


def once() -> int:
    """Single poll for cron / GitHub Actions (see watch.yml). Stateless
    except for LIVE_FLAG, the file that dedupes the one-time heads-up —
    on a runner with a fresh filesystem each run, that push may repeat.
    Exit codes: 0 = no alert, 42 = LIVE (urgent alert sent), 1 = poll failed."""
    sanity()
    try:
        data = narrow(fetch())
    except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
        log(f"poll failed: {e}")
        heartbeat(1, "poll failed")
        return 1
    signals = find_signals(data)
    if would_alert(signals, data):
        n_sx = screenx_count(data) if TARGET_EXPERIENCE else \
            sum(signals.values())
        log(f"LIVE — {n_sx}x {TARGET_EXPERIENCE or 'shows'}. Alerting.")
        notify(f"Spidey {TARGET_EXPERIENCE or 'shows'} LIVE",
               alert_message(data))
        heartbeat(1, f"{n_sx}x {TARGET_EXPERIENCE or 'shows'} LIVE — alerted")
        return 42
    if TARGET_EXPERIENCE and is_live(signals):
        notify_live_no_screenx(data)
        log(f"live, but no {TARGET_EXPERIENCE} yet — watching on")
        heartbeat(1, f"live, no {TARGET_EXPERIENCE} yet")
        return 0
    log("not live yet")
    heartbeat(1, "not live yet")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Pings your phone when target-date showtimes go live.")
    ap.add_argument("--check", action="store_true",
                    help="one dry-run poll, print diagnostics, send no alerts")
    ap.add_argument("--once", action="store_true",
                    help="one real poll for cron/CI (exit 42 = alerted)")
    ap.add_argument("--heartbeat", action="store_true",
                    help="low-priority status push per poll to NTFY_TOPIC-hb")
    ap.add_argument("--date", metavar="YYYY-MM-DD",
                    help="override PAYLOAD's date for this run and route all "
                         "pushes to NTFY_TOPIC-hb (test mode)")
    args = ap.parse_args()
    HEARTBEAT = args.heartbeat
    if args.date:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            ap.error(f"--date {args.date!r} is not a valid YYYY-MM-DD date")
        PAYLOAD["dated"] = args.date
        SAFE_MODE = True
        log(f"date override {args.date} — every push goes to {NTFY_TOPIC}-hb")
    if args.check:
        check_once()
    elif args.once:
        sys.exit(once())
    else:
        watch()

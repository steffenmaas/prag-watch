#!/usr/bin/env python3
"""
Prag IMAX watcher — Cinema City Flora, "Duna: část třetí" (Dune: Part Three) in 2D IMAX 70mm.

Two jobs:
  1. Detect the exact moment new bookable days are published (poll often, log timestamps).
  2. Detect when a relevant show still has enough free seats to be worth booking.

Uses only the public quickbook API (no auth, no scraping, ~1 KB per poll).
The seat-level API (/api/seats/*) sits behind a bot filter and is deliberately not used.

Usage:
    prag_watch.py check     # one poll; logs, alerts if warranted   (this is what launchd runs)
    prag_watch.py report    # analyse the log: publication times + sell-down curves
    prag_watch.py test      # send a test notification through every configured channel
    prag_watch.py status    # read-only summary: bookable shows + watcher health
"""

import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "log.jsonl")
STATE = os.path.join(HERE, "state.json")
CONFIG = os.path.join(HERE, "config.json")

TENANT = "10101"
CINEMA = "1052"          # Praha Flora, OC FLORA
# Matched against the Czech film name, case- and accent-insensitive. Use the
# full title: "dun" alone also catches "SLAVTE S NÁMI: Dunkerk", and plain
# "duna" would catch a 70mm re-run of Part One or Two.
FILM_NAME_HINT = "duna: cast treti"
ATTR_70MM = "70-mm"
API = "https://www.cinemacity.cz/cz/data-api-service/v1/quickbook"
# Fallback only - the real film page (id + slug) is learned from the API.
BOOKING_PAGE = "https://www.cinemacity.cz/cinemas/flora"
FILM = {"id": None, "link": None}


def fold(text):
    """Lowercase and strip accents: 'Duna: část třetí' -> 'duna: cast treti'."""
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).lower()


def booking_url(date=None):
    """Deep link to one day's showtimes at Flora. Czech only - www.cinemacity.cz
    has no English version (verified 04.08.2026: /en/, ?lang=en and an English
    Accept-Language header all return lang="cs")."""
    if not FILM["link"]:
        return BOOKING_PAGE
    if not date:
        return FILM["link"]
    return (f"{FILM['link']}#/buy-tickets-by-film?in-cinema={CINEMA}"
            f"&at={date}&for-movie={FILM['id']}&view-mode=list")


def seatplan_url(event_id):
    """Straight to one show's seat plan, in English.

    The booking engine (unlike the marketing site) does speak English.
    Note this is /order/<id>, NOT the /api/order/<id> link the API hands out -
    that one 404s. Verified 04.08.2026: returns lang="en", no error page.
    """
    return f"https://tickets.cinemacity.cz/order/{event_id}?lang=en"

# The Dune run spans the CEST -> CET switch, so use the real zone if available.
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Prague")
except Exception:  # noqa: BLE001 - Python < 3.9 or no tzdata
    TZ = timezone(timedelta(hours=1))

DEFAULTS = {
    # --- Two alert tiers ---
    # WAKE (emergency push, priority 2): hall less than 25% FILLED, i.e. more
    # than 75% of seats still free. That is the "freshly published, free pick of
    # rows 6-9" case - the only one worth being woken for. No compromises.
    "wake_ratio": 0.75,
    # INFO (normal push, no siren): worth knowing, not worth waking for.
    # Also covers every newly published day regardless of how full it already is.
    "info_ratio": 0.35,
    # Only affect ordering and which show gets linked - not the thresholds.
    "preferred_times": ["09:00"],
    "preferred_weekdays": [5, 6],   # Sat/Sun - no vacation day needed
    # From this date on only some weekdays are travelable (school term, work,
    # whatever constrains you). Before it, any weekday is fine.
    "restricted_from": "2099-01-01",
    "travel_weekdays": [4, 5],   # Mon=0 ... Fri=4, Sat=5
    # Stop watching once the 70mm run ends.
    "watch_until": "2027-02-28",
    # Re-alert about the same show at most once every N hours.
    "realert_hours": 12,
    "notify": {
        "ntfy_topic": None,          # e.g. "prag-imax-x7k2p9"
        "ntfy_server": "https://ntfy.sh",
        "pushover_token": None,      # app token
        "pushover_user": None,       # user key
        "pushover_emergency": True,  # priority 2: repeats until acknowledged
        "macos_local": True,
    },
}


# --------------------------------------------------------------------------- io

def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as fh:
                user = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"WARN: config.json unreadable ({exc}), using defaults", file=sys.stderr)
            user = {}
        for key, val in user.items():
            if key == "notify" and isinstance(val, dict):
                cfg["notify"].update(val)
            else:
                cfg[key] = val
    return cfg


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    return {"dates": [], "shows": {}, "alerted": {}, "last_log": 0}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, STATE)


def append_log(record):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def fetch(url, retries=3):
    """GET JSON with a browser-ish UA. Returns None on persistent failure."""
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36"),
        "Accept": "application/json",
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError, OSError) as exc:
            if attempt == retries - 1:
                print(f"WARN: fetch failed {url}: {exc}", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))
    return None


# ------------------------------------------------------------------------ api

def fetch_dates(until):
    url = f"{API}/{TENANT}/dates/in-cinema/{CINEMA}/until/{until}?attr=&lang=cs_CZ"
    data = fetch(url)
    if not data:
        return None
    return data.get("body", {}).get("dates", [])


def fetch_shows(date):
    """Return the 70mm shows matching FILM_NAME_HINT on `date` as {"HH:MM": {...}}."""
    url = f"{API}/{TENANT}/film-events/in-cinema/{CINEMA}/at-date/{date}?attr=&lang=cs_CZ"
    data = fetch(url)
    if not data:
        return None
    body = data.get("body", {})
    films = {f["id"]: f for f in body.get("films", [])}
    out = {}
    for ev in body.get("events", []):
        if ATTR_70MM not in ev.get("attributeIds", []):
            continue
        film = films.get(ev.get("filmId"), {})
        if FILM_NAME_HINT not in fold(film.get("name")):
            continue
        FILM["id"] = film.get("id")
        FILM["link"] = film.get("link")
        hhmm = ev.get("eventDateTime", "")[11:16]
        out[hhmm] = {
            "id": ev.get("id"),
            "ratio": ev.get("availabilityRatio"),
            "soldOut": ev.get("soldOut"),
        }
    return out


# ------------------------------------------------------------------- notify

def notify(cfg, title, message, urgent, url=None, url_title=None):
    """Fan out to every configured channel. Never raises."""
    n = cfg["notify"]
    sent = []
    url = url or booking_url()
    url_title = url_title or "Tickets Cinema City Flora"

    topic = n.get("ntfy_topic")
    if topic:
        try:
            server = n.get("ntfy_server", "https://ntfy.sh").rstrip("/")
            req = urllib.request.Request(
                f"{server}/{topic}",
                data=message.encode("utf-8"),
                headers={
                    "Title": title.encode("utf-8").decode("latin-1", "replace"),
                    "Priority": "urgent" if urgent else "default",
                    "Tags": "rotating_light" if urgent else "eyes",
                    "Click": url,
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=20).read()
            sent.append("ntfy")
        except Exception as exc:  # noqa: BLE001 - notification must never break the poll
            print(f"WARN: ntfy failed: {exc}", file=sys.stderr)

    if n.get("pushover_token") and n.get("pushover_user"):
        try:
            payload = {
                "token": n["pushover_token"],
                "user": n["pushover_user"],
                "title": title,
                "message": message,
                "url": url,
                "url_title": url_title,
            }
            prio = "0"
            if urgent and n.get("pushover_emergency", True):
                prio = "2"
                # Retries stop only when you tap "Acknowledge" in the app -
                # opening the notification or the booking link does NOT count.
                # So expire is the real safety net: long enough to wake you,
                # short enough not to punish you while you are already booking.
                payload.update({
                    "priority": "2",
                    "retry": str(n.get("pushover_retry", 60)),
                    "expire": str(n.get("pushover_expire", 1200)),
                })
            elif urgent:
                prio = "1"
                payload["priority"] = "1"
            if n.get("sound"):
                payload["sound"] = n["sound"]
            req = urllib.request.Request(
                "https://api.pushover.net/1/messages.json",
                data=urllib.parse.urlencode(payload).encode("utf-8"),
                method="POST",
            )
            raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8")
            api = json.loads(raw)
            # Pushover answers status:1 on success and returns a receipt for
            # priority 2 - without a receipt it was NOT an emergency message.
            if api.get("status") != 1:
                raise RuntimeError(f"API said: {raw}")
            note = f"pushover(prio={prio}"
            if prio == "2":
                note += f", receipt={'yes' if api.get('receipt') else 'MISSING'}"
            sent.append(note + ")")
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: pushover failed: {exc}", file=sys.stderr)

    # Outbox: alerts land in a JSON-lines file for someone else to deliver
    # (the cloud routine reads it and sends each entry as an e-mail).
    if n.get("outbox"):
        try:
            with open(os.path.join(HERE, n["outbox"]), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(TZ).isoformat(timespec="seconds"),
                    "title": title, "message": message, "urgent": urgent,
                    "url": url, "url_title": url_title,
                }, ensure_ascii=False) + "\n")
            sent.append("outbox")
        except OSError as exc:
            print(f"WARN: outbox failed: {exc}", file=sys.stderr)

    if n.get("macos_local"):
        try:
            import subprocess
            # AppleScript string literals have no \n escape - flatten first.
            flat = " · ".join(line.strip() for line in message.splitlines() if line.strip())
            flat = flat.replace('"', "'").replace("\\", "")[:220]
            safe_title = title.replace('"', "'").replace("\\", "")
            script = (f'display notification "{flat}" '
                      f'with title "{safe_title}" sound name "Sosumi"')
            res = subprocess.run(["osascript", "-e", script], timeout=15,
                                 check=False, capture_output=True)
            if res.returncode != 0:
                raise RuntimeError(res.stderr.decode("utf-8", "replace").strip())
            sent.append("macos")
            if urgent:
                # Fire and forget - never let audio stall the poll.
                try:
                    subprocess.Popen(
                        ["/usr/bin/afplay", "-v", "2",
                         "/System/Library/Sounds/Sosumi.aiff"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: macos notify failed: {exc}", file=sys.stderr)

    return sent


# -------------------------------------------------------------------- logic

def is_relevant(date_str, cfg):
    """Would you actually travel on this date?"""
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
        limit = datetime.strptime(cfg["restricted_from"], "%Y-%m-%d").date()
    except ValueError:
        return True
    if day < limit:
        return True
    return day.weekday() in cfg["travel_weekdays"]


def preference(key, cfg):
    """Rate a show key 'YYYY-MM-DDTHH:MM'. Returns (label, rank).

    Lower rank sorts first. Preference only decides ordering and which show the
    alert links to - the wake/info thresholds are the same for every show.
    """
    date_str, hhmm = key[:10], key[11:]
    try:
        weekday = datetime.strptime(date_str, "%Y-%m-%d").weekday()
    except ValueError:
        weekday = -1
    good_time = hhmm in cfg.get("preferred_times", [])
    good_day = weekday in cfg.get("preferred_weekdays", [])

    if good_time and good_day:
        return "★ WE + Wunschzeit", 0
    if good_day:
        return "★ Wochenende", 1
    if good_time:
        return "★ Wunschzeit 09:00", 2
    return "", 3


def cmd_check(cfg):
    now = datetime.now(TZ)
    if now.date() > datetime.strptime(cfg["watch_until"], "%Y-%m-%d").date():
        print(f"{now:%Y-%m-%d %H:%M} watch_until passed — nothing to do.")
        return 0

    state = load_state()
    # Look far ahead: premieres (and previews) are published months early.
    until = (now + timedelta(days=180)).strftime("%Y-%m-%d")

    dates = fetch_dates(until)
    if dates is None:
        append_log({"ts": now.isoformat(timespec="seconds"), "error": "dates_fetch_failed"})
        return 1

    known = set(state.get("dates", []))
    shows_now, show_ids = {}, {}
    film_dates = []
    for date in dates:
        shows = fetch_shows(date)
        if not shows:
            continue
        film_dates.append(date)
        for hhmm, info in shows.items():
            shows_now[f"{date}T{hhmm}"] = info["ratio"]
            show_ids[f"{date}T{hhmm}"] = info["id"]

    # A day only counts as "new" once it actually carries Dune 70mm shows.
    new_dates = sorted(set(film_dates) - known)
    # Cold start: the whole schedule looks "new". Seed silently instead of
    # firing a 3am alarm the first time the watcher (or a reinstall) runs.
    # Keyed on a flag, not on `known`: if the first run finds no days at all,
    # the first real publication must still wake you.
    cold_start = not state.get("seeded") and not known
    if cold_start:
        new_dates = []

    prev_shows = state.get("shows", {})
    changed = {k: v for k, v in shows_now.items() if prev_shows.get(k) != v}

    record = {"ts": now.isoformat(timespec="seconds")}
    if cold_start:
        record["seed"] = True
    if film_dates:
        record["horizon"] = max(film_dates)
        record["n_days"] = len(film_dates)
    if new_dates:
        record["new_dates"] = new_dates
    if changed:
        record["changed"] = {k: round(v, 4) if isinstance(v, float) else v
                             for k, v in changed.items()}
    # Always log something at least hourly so gaps mean "watcher was down".
    if changed or new_dates or (time.time() - state.get("last_log", 0)) > 3600:
        append_log(record)
        state["last_log"] = time.time()

    # ---- alerts
    alerts = []
    if new_dates:
        lines = []
        for date in new_dates:
            weekday_idx = datetime.strptime(date, "%Y-%m-%d").weekday()
            weekday = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][weekday_idx]
            marks = []
            for key in sorted(k for k in shows_now if k.startswith(date)):
                ratio = shows_now[key]
                star = "*" if key[11:] in cfg.get("preferred_times", []) else ""
                marks.append(f"{star}{key[11:]} {ratio:.0%} frei")
            tag = "  <-- WOCHENENDE" if weekday_idx in cfg.get("preferred_weekdays", []) else ""
            lines.append(f"{weekday} {date}: " + "  ".join(marks) + tag)
        # Link to the most preferred new show, not just the earliest day.
        best_key = min(
            (k for k in shows_now if k[:10] in new_dates),
            key=lambda k: (preference(k, cfg)[1], -(shows_now[k] or 0)))
        # ALWAYS a wake-up call, whatever the ratio says. Publication is the one
        # moment you can still pick rows 6-9, it happens maybe twice a week,
        # and the good rows are gone within days. A false wake costs one tap on
        # "Acknowledge"; a missed one costs the trip. Do not downgrade this to
        # info just because a batch shows up partly pre-sold - that case is
        # exactly when speed matters most.
        alerts.append((
            f"PRAG: {len(new_dates)} NEUE TAGE FREIGESCHALTET",
            "Neu freigeschaltet:\n" + "\n".join(lines) +
            f"\n\nHorizont jetzt {record.get('horizon','?')}."
            "\n\nErst in der App 'Acknowledge' tippen, dann Link oeffnen."
            f"\n\nAlle Zeiten am {best_key[8:10]}.{best_key[5:7]}. (tschechisch): "
            f"{booking_url(best_key[:10])}",
            True,
            seatplan_url(show_ids[best_key]),
            f"Sitzplan {best_key[8:10]}.{best_key[5:7]}. {best_key[11:]} (englisch)",
        ))

    def collect(threshold, tier):
        out = []
        for key, ratio in shows_now.items():
            if ratio is None or ratio < threshold:
                continue
            if not is_relevant(key[:10], cfg):
                continue
            last = state.get("alerted", {}).get(f"{tier}:{key}", 0)
            if time.time() - last < cfg["realert_hours"] * 3600:
                continue
            label, rank = preference(key, cfg)
            out.append((rank, key, ratio, label))
        out.sort(key=lambda g: (g[0], -g[2]))
        return out

    def render(rows):
        lines = []
        for _, key, ratio, label in rows:
            weekday = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][
                datetime.strptime(key[:10], "%Y-%m-%d").weekday()]
            suffix = f"  {label}" if label else ""
            filled = 1 - ratio
            lines.append(f"{weekday} {key[:10]} {key[11:]} — "
                         f"{ratio:.0%} frei ({filled:.0%} voll){suffix}")
        return "\n".join(lines)

    wake = collect(cfg["wake_ratio"], "wake")
    if wake:
        top = wake[0][1]
        alerts.append((
            "PRAG: SAAL FAST LEER",
            f"Unter {1 - cfg['wake_ratio']:.0%} belegt — freie Wahl in Reihe 6-9:\n"
            + render(wake) +
            "\n\nJetzt buchen. Erst in der App 'Acknowledge' tippen, dann Link oeffnen."
            f"\n\nAlle Zeiten am {top[8:10]}.{top[5:7]}. (tschechisch): "
            f"{booking_url(top[:10])}",
            True,
            seatplan_url(show_ids[top]),
            f"Sitzplan {top[8:10]}.{top[5:7]}. {top[11:]} (englisch)",
        ))
        for _, key, _, _ in wake:
            state.setdefault("alerted", {})[f"wake:{key}"] = time.time()

    # Info tier: everything above info_ratio that did not already trigger a wake.
    wake_keys = {k for _, k, _, _ in wake}
    info = [r for r in collect(cfg["info_ratio"], "info") if r[1] not in wake_keys]
    if info:
        top = info[0][1]
        alerts.append((
            "Prag: Plätze frei (kein Weckruf)",
            f"Über {cfg['info_ratio']:.0%} frei, aber unter deiner Weckschwelle "
            f"von {cfg['wake_ratio']:.0%}:\n" + render(info),
            False,
            seatplan_url(show_ids[top]),
            f"Sitzplan {top[8:10]}.{top[5:7]}. {top[11:]} (englisch)",
        ))
        for _, key, _, _ in info:
            state.setdefault("alerted", {})[f"info:{key}"] = time.time()

    for title, message, urgent, url, url_title in alerts:
        channels = notify(cfg, title, message, urgent, url, url_title)
        append_log({"ts": now.isoformat(timespec="seconds"),
                    "alert": title, "channels": channels})
        print(f"ALERT {title} -> {channels or 'no channel configured!'}")

    # Union, never replace: the API drops "today" between shows and re-adds it
    # later (seen 08.08.2026, 17:24-18:34) - forgetting a date would make its
    # return look like a fresh publication and fire a false wake-up alert.
    state["dates"] = sorted(known | set(film_dates))
    state["shows"] = shows_now
    state["seeded"] = True
    save_state(state)

    print(f"{now:%Y-%m-%d %H:%M} horizon={record.get('horizon','-')} "
          f"days={len(film_dates)} new={len(new_dates)} changed={len(changed)}")
    return 0


def cmd_report():
    if not os.path.exists(LOG):
        print("No log yet.")
        return 0
    records = []
    with open(LOG, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    if not records:
        print("Log is empty.")
        return 0

    print(f"Log: {len(records)} Einträge, {records[0]['ts']} bis {records[-1]['ts']}\n")

    print("=== VERÖFFENTLICHUNGEN (wann kamen neue Tage rein?) ===")
    pubs = [r for r in records if r.get("new_dates")]
    if not pubs:
        print("  noch keine beobachtet\n")
    for rec in pubs:
        stamp = datetime.fromisoformat(rec["ts"])
        days = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][stamp.weekday()]
        print(f"  {days} {stamp:%Y-%m-%d %H:%M} — +{len(rec['new_dates'])} Tage "
              f"({rec['new_dates'][0]}…{rec['new_dates'][-1]}), Horizont {rec.get('horizon','?')}")
    if len(pubs) >= 2:
        gaps = [(datetime.fromisoformat(b["ts"]) - datetime.fromisoformat(a["ts"]))
                for a, b in zip(pubs, pubs[1:])]
        avg = sum(g.total_seconds() for g in gaps) / len(gaps) / 3600
        print(f"\n  Abstand zwischen Freischaltungen: "
              f"{', '.join(f'{g.total_seconds()/3600:.1f}h' for g in gaps)} (Ø {avg:.1f}h)")
        hours = sorted({datetime.fromisoformat(p['ts']).hour for p in pubs})
        print(f"  Beobachtete Uhrzeiten: {hours}")
    print()

    print("=== AUSVERKAUFS-TEMPO (wie schnell fallen frische Tage?) ===")
    first_seen, curves = {}, {}
    for rec in records:
        stamp = datetime.fromisoformat(rec["ts"])
        for key, ratio in (rec.get("changed") or {}).items():
            if not isinstance(ratio, (int, float)):
                continue
            first_seen.setdefault(key, stamp)
            curves.setdefault(key, []).append((stamp, ratio))
    if not curves:
        print("  noch keine Bewegung beobachtet\n")
    else:
        rows = []
        for key, points in curves.items():
            if len(points) < 2:
                continue
            (t0, r0), (t1, r1) = points[0], points[-1]
            hours = max((t1 - t0).total_seconds() / 3600, 0.1)
            rows.append((key, r0, r1, hours, (r0 - r1) / hours))
        rows.sort(key=lambda r: -r[4])
        print(f"  {'Vorstellung':<20}{'erst':>7}{'jetzt':>8}{'über':>8}{'Verlust/h':>11}")
        for key, r0, r1, hours, rate in rows[:15]:
            print(f"  {key:<20}{r0:>6.0%}{r1:>8.0%}{hours:>7.0f}h{rate:>10.1%}")
    print()

    errs = [r for r in records if r.get("error")]
    if errs:
        print(f"=== FEHLER: {len(errs)} (zuletzt {errs[-1]['ts']}) ===")
    return 0


def cmd_status(cfg):
    """Read-only weekly summary: what is bookable right now and is the watcher
    alive? Touches neither state.json nor log.jsonl."""
    now = datetime.now(TZ)
    print(f"Stand {now:%d.%m.%Y %H:%M} - Dune: Part Three, 70 mm IMAX, Cinema City Flora\n")

    until = (now + timedelta(days=180)).strftime("%Y-%m-%d")
    dates = fetch_dates(until)
    if dates is None:
        print("SPIELPLAN: API nicht erreichbar.\n")
    else:
        rows = []
        for date in dates:
            for hhmm, info in sorted((fetch_shows(date) or {}).items()):
                rows.append((date, hhmm, info))
        if not rows:
            print("SPIELPLAN: keine 70-mm-Vorstellungen freigeschaltet.\n")
        else:
            print(f"SPIELPLAN ({len({r[0] for r in rows})} Tage, {len(rows)} Vorstellungen):")
            for date, hhmm, info in rows:
                weekday = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][
                    datetime.strptime(date, "%Y-%m-%d").weekday()]
                ratio = info["ratio"]
                if info.get("soldOut") or not ratio:
                    free = "ausverkauft"
                else:
                    free = f"{ratio:.0%} frei" + ("  <-- BUCHBAR" if ratio >= cfg["info_ratio"] else "")
                print(f"  {weekday} {date[8:10]}.{date[5:7]}. {hhmm}  {free}  {seatplan_url(info['id'])}")
            print(f"\nAlle Zeiten: {booking_url()}\n")

    records = []
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8") as fh:
            for line in fh:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    week_ago = now - timedelta(days=7)
    recent = [r for r in records if datetime.fromisoformat(r["ts"]) >= week_ago]
    print("WAECHTER:")
    if records:
        last = datetime.fromisoformat(records[-1]["ts"])
        age_h = (now - last).total_seconds() / 3600
        print(f"  letzter Logeintrag: {last.astimezone(TZ):%d.%m. %H:%M} "
              f"(vor {age_h:.1f} h){'  <-- WAECHTER LAEUFT NICHT?' if age_h > 3 else ''}")
    else:
        print("  noch kein Logeintrag")
    print(f"  letzte 7 Tage: {len(recent)} Logeintraege, "
          f"{sum(1 for r in recent if r.get('new_dates'))} Freischaltungen, "
          f"{sum(1 for r in recent if r.get('alert'))} Alarme, "
          f"{sum(1 for r in recent if r.get('error'))} Fehler")
    print(f"  laeuft bis: {cfg['watch_until']}")
    return 0


def cmd_test(cfg, only=None):
    """Send a test alert. `only` restricts to one channel so a Mac sound
    cannot be mistaken for a phone sound during diagnosis."""
    if only:
        cfg = json.loads(json.dumps(cfg))
        n = cfg["notify"]
        if only == "pushover":
            n["macos_local"] = False
            n["ntfy_topic"] = None
        elif only == "ntfy":
            n["macos_local"] = False
            n["pushover_token"] = None
        elif only == "macos":
            n["ntfy_topic"] = None
            n["pushover_token"] = None
        else:
            print(f"Unbekannter Kanal '{only}' — erlaubt: pushover, ntfy, macos")
            return 2
        print(f"NUR Kanal '{only}' — alle anderen fuer diesen Test abgeschaltet.")
        if only == "pushover":
            print("Der Mac bleibt still. Was du jetzt hoerst, kommt vom Handy.\n")

    channels = notify(
        cfg, "Prag-Wächter Test",
        "Wenn du das siehst (und hörst), funktioniert der Alarm. "
        "Bei echten Treffern kommt dieselbe Meldung mit den freien Vorstellungen.",
        urgent=True)
    if channels:
        print(f"Testbenachrichtigung raus über: {', '.join(channels)}")
    else:
        print("Kein Kanal konfiguriert — config.json anlegen (siehe README.md).")
    return 0


def cmd_setup_pushover():
    """Write Pushover credentials into config.json without them touching a chat log."""
    print("Pushover einrichten. Beide Werte stehen auf pushover.net:")
    print("  User Key   -> Startseite, oben rechts ('Your User Key')")
    print("  API Token  -> pushover.net/apps/build, App anlegen (z.B. 'Prag Waechter')")
    print()
    user = input("User Key : ").strip()
    token = input("API Token: ").strip()
    if not user or not token:
        print("Abgebrochen - beide Werte werden gebraucht.")
        return 1

    existing = {}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as fh:
                existing = json.load(fh)
        except (OSError, ValueError):
            pass
    notify = existing.get("notify", {})
    notify.update({
        "pushover_user": user,
        "pushover_token": token,
        "pushover_emergency": True,
    })
    existing["notify"] = notify
    with open(CONFIG, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2, ensure_ascii=False)
    os.chmod(CONFIG, 0o600)
    print(f"\nGespeichert in {CONFIG} (nur fuer dich lesbar, gitignored).")
    print("Jetzt testen:  python3 prag_watch.py test")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "setup-pushover":
        return cmd_setup_pushover()
    cfg = load_config()
    if cmd == "check":
        return cmd_check(cfg)
    if cmd == "report":
        return cmd_report()
    if cmd == "status":
        return cmd_status(cfg)
    if cmd == "test":
        return cmd_test(cfg, sys.argv[2] if len(sys.argv) > 2 else None)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

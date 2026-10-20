#!/usr/bin/env python3
"""Free BLR -> DEL flight tracker (standard library only).

Two things are logged on every run:
  1. MORNING DELAYS  - EVERY flight (departures and arrivals, all airlines, all routes) at the
                       home airport between 06:00 and 09:00 today: delay in minutes + status.
  2. DEL OPTIONS     - every BLR -> DEL option on the travel date:
                       direct flights (all airlines, from the live airport board) and
                       SpiceJet connecting itineraries (from schedule_blr_del.csv, with
                       each leg's live status looked up).

Data source: AeroDataBox free plan (RapidAPI). Needs env var RAPIDAPI_KEY.
Use --mock to run the whole pipeline on synthetic data (no key, writes to ./_mock/).
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
DAY_NAMES = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
MIN_CONNECTION = dt.timedelta(minutes=45)
EST_LEG1_BLOCK = dt.timedelta(minutes=135)  # BLR->BOM block time, used only if arrival unknown

# Higher = worse. Used to pick the overall state of a multi-leg itinerary.
RANK = {"CANCELLED": 6, "CANCELLED?": 5, "NOT IN FEED": 4, "DIVERTED": 3.5,
        "MISCONNECT RISK": 3, "DELAYED": 2, "ON TIME": 1, "SCHEDULED": 0.5}
BAD_STATES = {"CANCELLED", "CANCELLED?", "NOT IN FEED", "DIVERTED"}
LIVE_STATUSES = {"CheckIn", "Boarding", "GateClosed", "Departed", "EnRoute", "Approaching", "Arrived"}


def nflight(s):
    """'SG  141' / 'sg141' -> 'SG141'"""
    return "".join((s or "").split()).upper()


def parse_ts(s):
    """AeroDataBox time strings: '2026-10-28 06:05+05:30' or '2026-10-28 00:35Z'."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def tfield(obj):
    """{'utc': ..., 'local': ...} -> aware datetime (prefers utc, falls back to local)."""
    if not obj:
        return None
    return parse_ts(obj.get("utc")) or parse_ts(obj.get("local"))


def hhmm(d, tz):
    return d.astimezone(tz).strftime("%H:%M") if d else ""


def minutes(a, b):
    """b - a in whole minutes (None if either missing)."""
    return int(round((b - a).total_seconds() / 60)) if a and b else None


def operates_on(row, date):
    if not (row["valid_from"] <= date.isoformat() <= row["valid_till"]):
        return False
    return row["days"] == "DAILY" or DAY_NAMES[date.weekday()] in row["days"]


def sheet_time(date, text, tz):
    t = dt.datetime.strptime(text.strip(), "%I:%M %p").time()
    return dt.datetime.combine(date, t, tzinfo=tz)


def load_schedule(path=None):
    path = path or os.path.join(ROOT, "schedule_blr_del.csv")
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


# ----------------------------------------------------------------------------------
# AeroDataBox client + response normaliser
# ----------------------------------------------------------------------------------
class BudgetExceeded(Exception):
    pass


class ApiError(Exception):
    pass


def norm_flight(item, home=None, direction="Departure"):
    """Normalise one AeroDataBox flight object (FIDS item or flight-by-number item).

    FIDS items describe the queried airport in `movement` (its `airport` is the OTHER end);
    flight-by-number items use `departure` / `arrival`. Both shapes are handled.
    """
    mv = item.get("movement") or {}
    dp = item.get("departure") or {}
    ar = item.get("arrival") or {}
    airline = (item.get("airline") or {}).get("name") or ""
    number = nflight(item.get("number"))

    dp_iata = (dp.get("airport") or {}).get("iata")
    ar_iata = (ar.get("airport") or {}).get("iata")
    mv_iata = (mv.get("airport") or {}).get("iata")
    origin = dp_iata or home
    dest = next((c for c in (ar_iata, mv_iata) if c and c != home), None)

    rec = {
        "number": number,
        "airline": airline,
        "status": item.get("status") or "Unknown",
        "direction": direction,
        "origin": origin,
        "dest": dest,
        "s_dep": tfield(mv.get("scheduledTime")) or tfield(dp.get("scheduledTime")),
        "r_dep": tfield(mv.get("revisedTime")) or tfield(dp.get("revisedTime")),
        "s_arr": tfield(ar.get("scheduledTime")),
        "r_arr": tfield(ar.get("revisedTime")),
    }
    if direction == "Arrival":
        # movement = arrival at the queried airport; movement.airport = where it came from
        rec.update(origin=mv_iata, dest=home, s_dep=None, r_dep=None,
                   s_arr=tfield(mv.get("scheduledTime")) or tfield(ar.get("scheduledTime")),
                   r_arr=tfield(mv.get("revisedTime")) or tfield(ar.get("revisedTime")))
    return rec


class AeroDataBox:
    # Unit costs on the free plan (FIDS is documented as Tier 2 = 2 units).
    COST_FIDS = 2
    COST_NUMBER = 2

    def __init__(self, key, budget=40, host=None, raw_dir=None):
        self.key = (key or "").strip()  # secrets pasted with a trailing newline break HTTP headers
        self.host = (host or os.environ.get("AERODATABOX_HOST", "aerodatabox.p.rapidapi.com")).strip()
        self.budget = budget
        self.units = 0
        self.raw_dir = raw_dir
        self._last = 0.0

    def _get(self, path, params, cost, label):
        if self.units + cost > self.budget:
            raise BudgetExceeded(f"unit budget {self.budget} reached before '{label}'")
        qs = urllib.parse.urlencode(params)
        url = f"https://{self.host}{path}" + (f"?{qs}" if qs else "")
        headers = {"X-RapidAPI-Key": self.key, "X-RapidAPI-Host": self.host,
                   "Accept": "application/json"}
        for attempt in range(3):
            wait = 1.1 - (time.time() - self._last)  # free plan: 1 request / second
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                    body = r.read()
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                if e.code == 404:
                    self.units += cost
                    return None
                raise ApiError(f"{label}: HTTP {e.code} {e.read()[:200]!r}") from None
            except urllib.error.URLError as e:
                raise ApiError(f"{label}: {e.reason}") from None
            except ValueError as e:  # e.g. malformed header/url, never echo the key
                raise ApiError(f"{label}: bad request ({e.__class__.__name__}); check the RAPIDAPI_KEY secret") from None
            self.units += cost
            if not body:
                return None
            data = json.loads(body)
            self._dump(label, data)
            return data
        raise ApiError(f"{label}: rate limited")

    def _dump(self, label, data):
        if not self.raw_dir:
            return
        os.makedirs(self.raw_dir, exist_ok=True)
        with open(os.path.join(self.raw_dir, f"{label}.json"), "w") as f:
            json.dump(data, f, indent=1)

    def airport_departures(self, iata, start_local, end_local):
        """start/end like '2026-10-28T06:00'; window must be <= 12 h."""
        data = self._get(
            f"/flights/airports/iata/{iata}/{start_local}/{end_local}",
            {"direction": "Departure", "withLeg": "true", "withCancelled": "true",
             "withCodeshared": "false", "withCargo": "false", "withPrivate": "false",
             "withLocation": "false"},
            self.COST_FIDS, f"fids_{iata}_{start_local.replace(':', '')}")
        return [norm_flight(i, home=iata) for i in (data or {}).get("departures", [])]

    def airport_window(self, iata, start_local, end_local):
        """Departures AND arrivals in one call (used for the morning-delay metric)."""
        data = self._get(
            f"/flights/airports/iata/{iata}/{start_local}/{end_local}",
            {"direction": "Both", "withLeg": "true", "withCancelled": "true",
             "withCodeshared": "false", "withCargo": "false", "withPrivate": "false",
             "withLocation": "false"},
            self.COST_FIDS, f"fids_both_{iata}_{start_local.replace(':', '')}")
        data = data or {}
        return ([norm_flight(i, home=iata) for i in data.get("departures", [])] +
                [norm_flight(i, home=iata, direction="Arrival") for i in data.get("arrivals", [])])

    def flight_by_number(self, number, date_iso):
        data = self._get(
            f"/flights/number/{number}/{date_iso}",
            {"withAircraftImage": "false", "withLocation": "false"},
            self.COST_NUMBER, f"number_{number}_{date_iso}")
        items = data if isinstance(data, list) else (data or {}).get("flights", [])
        return [norm_flight(i) for i in items]


# ----------------------------------------------------------------------------------
# classification
# ----------------------------------------------------------------------------------
def make_classifier(threshold):
    def classify(status, delay):
        if status == "Canceled":
            return "CANCELLED"
        if status == "CanceledUncertain":
            return "CANCELLED?"
        if status == "Diverted":
            return "DIVERTED"
        if status == "Delayed" or (delay is not None and delay >= threshold):
            return "DELAYED"
        if delay is not None or status in LIVE_STATUSES:
            return "ON TIME"
        return "SCHEDULED"
    return classify


# ----------------------------------------------------------------------------------
# metric 1: morning delays
# ----------------------------------------------------------------------------------
def morning(client, cfg, date, tz, classify):
    """Metric 1: EVERY flight (all airlines, all routes) at the home airport in the window."""
    lo, hi = cfg["morning_window"]
    home = cfg["home_airport"]
    if cfg.get("morning_include_arrivals", True):
        flights = client.airport_window(home, f"{date}T{lo}", f"{date}T{hi}")
    else:
        flights = client.airport_departures(home, f"{date}T{lo}", f"{date}T{hi}")
    rows, seen = [], set()
    for f in flights:
        arrival = f["direction"] == "Arrival"
        sched, rev = (f["s_arr"], f["r_arr"]) if arrival else (f["s_dep"], f["r_dep"])
        if not sched:
            continue
        local = sched.astimezone(tz)
        if not (lo <= local.strftime("%H:%M") <= hi):
            continue
        key = (f["number"], f["direction"], local.strftime("%H:%M"))
        if key in seen:
            continue
        seen.add(key)
        delay = minutes(sched, rev)
        rows.append({
            "date": date.isoformat(), "direction": f["direction"], "flight": f["number"],
            "airline": f["airline"], "airport": (f["origin"] if arrival else f["dest"]) or "",
            "sched": hhmm(sched, tz), "revised": hhmm(rev, tz),
            "delay_min": "" if delay is None else delay,
            "status": f["status"], "state": classify(f["status"], delay),
        })
    rows.sort(key=lambda r: (r["sched"], r["direction"]))
    return rows


# ----------------------------------------------------------------------------------
# metric 2: BLR -> DEL options
# ----------------------------------------------------------------------------------
def fetch_day(client, home, date):
    out = []
    for lo, hi in (("00:00", "11:59"), ("12:00", "23:59")):
        out += client.airport_departures(home, f"{date}T{lo}", f"{date}T{hi}")
    return out


def worst(states):
    return max(states, key=lambda s: RANK.get(s, 0))


def leg_text(label, f, tz, classify, date=None):
    if not f:
        return f"{label} NOT IN FEED"
    d = minutes(f["s_dep"], f["r_dep"])
    extra = f" {d:+d}m" if d else ""
    nxt = ""
    if date and f["s_dep"]:
        off = (f["s_dep"].astimezone(tz).date() - date).days
        nxt = f" (+{off}d)" if off else ""
    return f"{label} {hhmm(f['s_dep'], tz)}{nxt} {classify(f['status'], d)}{extra}"


def options(client, cfg, schedule, date, tz, classify, with_legs=True):
    home, dest, mine = cfg["home_airport"], cfg["dest_airport"], nflight(cfg.get("my_flight"))
    day = fetch_day(client, home, date)
    by_num = {}
    for f in day:
        by_num.setdefault(f["number"], f)
    todays = [r for r in schedule if operates_on(r, date)]
    rows, seen = [], set()

    def add(kind, itin, airline, via, dep, arr, state, delay, detail):
        rows.append({"kind": kind, "itinerary": itin, "airline": airline, "via": via,
                     "dep": dep, "arr": arr, "state": state,
                     "delay_min": "" if delay is None else delay, "detail": detail,
                     "mine": "yes" if mine and mine in itin.split("+") else ""})

    # -- direct flights seen on the live airport board (all airlines) ----------------
    for f in day:
        if f["dest"] == dest and f["number"] not in seen:
            seen.add(f["number"])
            d = minutes(f["s_dep"], f["r_dep"])
            st = classify(f["status"], d)
            add("direct", f["number"], f["airline"], "", hhmm(f["s_dep"], tz),
                hhmm(f["s_arr"], tz), st, d, f"API status: {f['status']}")

    # -- SpiceJet direct / via-stop flights the schedule file says should operate -----
    for r in todays:
        if r["kind"] not in ("direct", "via_stop"):
            continue
        num = r["flights"]
        if num in seen:
            continue
        seen.add(num)
        f = by_num.get(num)
        kind = "direct" if r["kind"] == "direct" else f"one-stop (via {r['via'].title()})"
        if f:
            d = minutes(f["s_dep"], f["r_dep"])
            add(kind, num, "SpiceJet", r["via"].title(), hhmm(f["s_dep"], tz), r["arr"],
                classify(f["status"], d), d, f"API status: {f['status']}")
        else:
            add(kind, num, "SpiceJet", r["via"].title(), sheet_time(date, r["dep"], tz).strftime("%H:%M"),
                r["arr"], "NOT IN FEED", None,
                "In SpiceJet schedule file but missing from the live airport board")

    # -- make sure your own flight is always represented -------------------------------
    if mine and mine not in seen:
        add("direct", mine, "", "", "", "", "NOT IN FEED", None,
            "Your flight is not on the live airport board for this date")

    # -- SpiceJet connecting itineraries ----------------------------------------------
    if with_legs:
        l2_cache = {}

        def leg2_instances(num):
            if num not in l2_cache:
                inst = []
                for d in (date, date + dt.timedelta(days=1)):
                    inst += client.flight_by_number(num, d.isoformat())
                l2_cache[num] = [i for i in inst if i["s_dep"]]
            return l2_cache[num]

        for r in todays:
            if r["kind"] != "connecting":
                continue
            n1, n2 = r["flights"].split("|")
            via = r["via_iata"]
            l1 = by_num.get(n1)
            sheet_dep = sheet_time(date, r["dep"], tz)
            if l1 and l1["s_dep"]:
                arr1 = l1["s_arr"] or (l1["s_dep"] + EST_LEG1_BLOCK)
                rarr1 = l1["r_arr"] or (arr1 + dt.timedelta(minutes=minutes(l1["s_dep"], l1["r_dep"]) or 0))
            else:
                arr1 = rarr1 = sheet_dep + EST_LEG1_BLOCK
            cands = [i for i in leg2_instances(n2)
                     if (not i["origin"] or i["origin"] == via) and (not i["dest"] or i["dest"] == dest)]
            cands.sort(key=lambda i: i["s_dep"])
            l2 = next((i for i in cands if i["s_dep"] >= arr1 + MIN_CONNECTION), None)

            d1 = minutes(l1["s_dep"], l1["r_dep"]) if l1 else None
            d2 = minutes(l2["s_dep"], l2["r_dep"]) if l2 else None
            s1 = classify(l1["status"], d1) if l1 else "NOT IN FEED"
            s2 = classify(l2["status"], d2) if l2 else "NOT IN FEED"
            states = [s1, s2]
            if l2 and s1 not in BAD_STATES and rarr1 + MIN_CONNECTION > (l2["r_dep"] or l2["s_dep"]):
                states.append("MISCONNECT RISK")
            overall = worst(states)
            arr_txt = hhmm(l2["s_arr"], tz) if l2 and l2["s_arr"] else r["arr"]
            detail = " | ".join([leg_text(f"{n1} BLR>{via}", l1, tz, classify),
                                 leg_text(f"{n2} {via}>DEL", l2, tz, classify, date)])
            if overall == "MISCONNECT RISK":
                detail += " | leg 1 arrival + 45 min is later than leg 2 departure"
            add("connecting", f"{n1}+{n2}", "SpiceJet", r["via"].title(),
                hhmm(l1["s_dep"], tz) if l1 else sheet_dep.strftime("%H:%M"),
                arr_txt, overall, d1, detail)

    rows.sort(key=lambda r: (r["dep"] or "99:99", r["itinerary"]))
    return rows


# ----------------------------------------------------------------------------------
# storage, alerts
# ----------------------------------------------------------------------------------
MORNING_COLS = ["run_utc", "date", "direction", "flight", "airline", "airport", "sched",
                "revised", "delay_min", "status", "state"]
OPTION_COLS = ["run_utc", "travel_date", "kind", "itinerary", "airline", "via", "dep", "arr",
               "state", "delay_min", "detail", "mine"]
RUN_COLS = ["run_utc", "units_used", "morning_rows", "option_rows", "errors"]


def append_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def previous_states(path, travel_date):
    rows = [r for r in read_csv(path) if r["travel_date"] == travel_date]
    if not rows:
        return {}
    last = max(r["run_utc"] for r in rows)
    return {r["itinerary"]: r["state"] for r in rows if r["run_utc"] == last}


def build_alerts(cfg, prev, rows):
    thr = cfg["delay_threshold_min"]
    lines, mine_hit = [], False
    for r in rows:
        old = prev.get(r["itinerary"])
        new = r["state"]
        delay = int(r["delay_min"]) if str(r["delay_min"]).lstrip("-").isdigit() else 0
        if new in BAD_STATES and old != new:
            lines.append(f"{r['itinerary']} ({r['dep']}): {new}" + (" <- YOUR FLIGHT" if r["mine"] else ""))
        elif r["mine"] and new == "DELAYED" and old != "DELAYED" and delay >= thr:
            lines.append(f"{r['itinerary']}: delayed {delay} min <- YOUR FLIGHT")
        mine_hit = mine_hit or (r["mine"] and (new in BAD_STATES or new == "DELAYED") and old != new)
    return lines, mine_hit


def send_alert(topic, lines, urgent, travel_date):
    if not lines:
        return
    msg = "\n".join(lines)
    print("ALERT:\n" + msg)
    if not topic:
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=msg.encode(), method="POST",
        headers={"Title": f"BLR-DEL {travel_date}: flight status change",
                 "Priority": "5" if urgent else "3", "Tags": "airplane"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # alert failure must not fail the run
        print(f"ntfy failed: {e}", file=sys.stderr)


# ----------------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mock", action="store_true", help="synthetic data, no API key, writes to ./_mock")
    ap.add_argument("--mock-cancel", default="", help="with --mock: comma list of flights to show cancelled")
    ap.add_argument("--skip-morning", action="store_true", help="skip metric 1 (saves 2 API units)")
    ap.add_argument("--skip-legs", action="store_true", help="skip connecting-leg lookups (saves ~8 units)")
    ap.add_argument("--date", help="pretend today is YYYY-MM-DD")
    ap.add_argument("--base", default=ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    with open(os.path.join(ROOT, "config.json")) as f:
        cfg = json.load(f)
    tz = ZoneInfo(cfg["timezone"])
    now = dt.datetime.now(tz)
    today = dt.date.fromisoformat(args.date) if args.date else now.date()
    travel = dt.date.fromisoformat(cfg["travel_date"])
    classify = make_classifier(cfg["delay_threshold_min"])
    schedule = load_schedule()

    base = os.path.join(args.base, "_mock") if args.mock else args.base
    data_dir, docs_dir = os.path.join(base, "data"), os.path.join(base, "docs")

    if args.mock:
        from mock_client import MockClient
        client = MockClient(schedule, tz, cancel={nflight(x) for x in args.mock_cancel.split(",") if x})
    else:
        key = (os.environ.get("RAPIDAPI_KEY") or "").strip()
        if not key:
            sys.exit("RAPIDAPI_KEY is not set (add it as a GitHub secret, or use --mock).")
        client = AeroDataBox(key, budget=cfg["max_units_per_run"],
                             raw_dir=os.path.join(data_dir, "raw"))

    run_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    errors, m_rows, o_rows = [], [], []

    if not args.skip_morning:
        try:
            m_rows = morning(client, cfg, today, tz, classify)
            append_csv(os.path.join(data_dir, "morning_delays.csv"), MORNING_COLS,
                       [dict(r, run_utc=run_utc) for r in m_rows])
        except (ApiError, BudgetExceeded) as e:
            errors.append(f"morning: {e}")

    if today <= travel:
        try:
            o_rows = options(client, cfg, schedule, travel, tz, classify, with_legs=not args.skip_legs)
            path = os.path.join(data_dir, "options.csv")
            prev = previous_states(path, travel.isoformat())
            append_csv(path, OPTION_COLS,
                       [dict(r, run_utc=run_utc, travel_date=travel.isoformat()) for r in o_rows])
            lines, urgent = build_alerts(cfg, prev, o_rows) if prev else ([], False)
            send_alert((os.environ.get("NTFY_TOPIC") or "").strip(), lines, urgent, travel.isoformat())
        except (ApiError, BudgetExceeded) as e:
            errors.append(f"options: {e}")

    units = getattr(client, "units", 0)
    append_csv(os.path.join(data_dir, "runs.csv"), RUN_COLS,
               [{"run_utc": run_utc, "units_used": units, "morning_rows": len(m_rows),
                 "option_rows": len(o_rows), "errors": "; ".join(errors)}])

    from dashboard import render
    render(cfg, data_dir, docs_dir, tz, mock=args.mock)
    print(f"done: {len(m_rows)} morning flights, {len(o_rows)} options, {units} API units used")
    for e in errors:
        print("ERROR " + e, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

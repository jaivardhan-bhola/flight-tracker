"""Builds docs/index.html (a static, dependency-free dashboard) from the CSV logs."""
import csv
import datetime as dt
import html
import os
import statistics

STATE_CLASS = {"CANCELLED": "bad", "CANCELLED?": "bad", "NOT IN FEED": "bad", "DIVERTED": "bad",
               "MISCONNECT RISK": "warn", "DELAYED": "warn", "ON TIME": "ok", "SCHEDULED": "muted"}


def _read(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _e(x):
    return html.escape(str(x))


def _chip(state):
    return f'<span class="chip {STATE_CLASS.get(state, "muted")}">{_e(state)}</span>'


def _delay(v):
    return f"{int(v):+d} min" if str(v).lstrip("-").isdigit() else "—"


def _latest_per(rows, key_fields):
    """Keep only the newest run's row for each key."""
    best = {}
    for r in rows:
        k = tuple(r[f] for f in key_fields)
        if k not in best or r["run_utc"] >= best[k]["run_utc"]:
            best[k] = r
    return list(best.values())


def _history(all_rows, itinerary, n=5):
    """Compressed state history, oldest -> newest, e.g. 'SCHEDULED > DELAYED'."""
    seq = []
    for r in sorted((r for r in all_rows if r["itinerary"] == itinerary), key=lambda r: r["run_utc"]):
        if not seq or seq[-1] != r["state"]:
            seq.append(r["state"])
    return " → ".join(seq[-n:])


def render(cfg, data_dir, docs_dir, tz, mock=False):
    thr = cfg["delay_threshold_min"]
    travel = cfg["travel_date"]
    morning = _read(os.path.join(data_dir, "morning_delays.csv"))
    opts_all = _read(os.path.join(data_dir, "options.csv"))
    runs = _read(os.path.join(data_dir, "runs.csv"))
    opts = [r for r in opts_all if r["travel_date"] == travel]
    last_run = max((r["run_utc"] for r in opts), default="")
    opts = [r for r in opts if r["run_utc"] == last_run]

    # ---- metric 1: per-day summary + latest day's detail ----
    newest = {}
    for r in morning:  # use only each day's most recent run (an older run may include flights since filtered out)
        newest[r["date"]] = max(newest.get(r["date"], ""), r["run_utc"])
    latest = [r for r in morning if r["run_utc"] == newest[r["date"]] and r["direction"] == "Departure"]
    days = sorted({r["date"] for r in latest}, reverse=True)
    summ = []
    for d in days:
        rs = [r for r in latest if r["date"] == d]
        ds = [int(r["delay_min"]) for r in rs if str(r["delay_min"]).lstrip("-").isdigit()]
        late = [x for x in ds if x >= thr]
        canc = [r for r in rs if r["state"].startswith("CANCELLED")]
        summ.append(f"<tr><td>{_e(d)}</td><td>{len(rs)}</td>"
                    f"<td>{round(statistics.mean(ds)) if ds else '—'}</td>"
                    f"<td>{(str(round(100 * len(late) / len(ds))) + '%') if ds else '—'}</td>"
                    f"<td>{max(ds) if ds else '—'}</td><td>{len(canc)}</td></tr>")
    detail = ""
    if days:
        d = days[0]
        rs = sorted((r for r in latest if r["date"] == d), key=lambda r: (r["sched"], r["direction"]))
        body = "".join(
            f"<tr class='{'mine' if r['flight'] == cfg.get('my_flight') else ''}'><td>{_e(r['sched'])}</td>"
            f"<td>{'Dep' if r['direction'] == 'Departure' else 'Arr'}</td><td>{_e(r['flight'])}</td>"
            f"<td>{_e(r['airline'])}</td><td>{_e(r['airport'])}</td><td>{_delay(r['delay_min'])}</td>"
            f"<td>{_chip(r['state'])}</td></tr>" for r in rs)
        detail = (f"<details open><summary>All {len(rs)} flights, {_e(d)} "
                  f"({cfg['morning_window'][0]}–{cfg['morning_window'][1]})</summary>"
                  f"<div class='scroll'><table><tr><th>Sched</th><th></th><th>Flight</th><th>Airline</th>"
                  f"<th>{_e(cfg['home_airport'])} ↔</th><th>Delay</th><th>State</th></tr>{body}</table></div></details>")

    # ---- metric 2: options for the travel date ----
    opt_rows = ""
    for r in sorted(opts, key=lambda r: (r["dep"] or "99", r["itinerary"])):
        cls = "mine" if r["mine"] else ""
        opt_rows += (f"<tr class='{cls}'><td>{_e(r['dep'])}</td><td>{_e(r['itinerary'])}"
                     f"{' <b>(yours)</b>' if r['mine'] else ''}<div class='sub'>{_e(r['kind'])} · {_e(r['airline'])}"
                     f"{' · via ' + _e(r['via']) if r['via'] else ''}</div></td>"
                     f"<td>{_chip(r['state'])}<div class='sub'>{_e(r['detail'])}</div></td>"
                     f"<td class='sub'>{_e(_history(opts_all, r['itinerary']))}</td></tr>")
    mine_row = next((r for r in opts if r["mine"]), None)
    hero = ""
    if mine_row:
        hero = (f"<div class='hero {STATE_CLASS.get(mine_row['state'], 'muted')}'>"
                f"<div class='k'>Your flight {_e(mine_row['itinerary'])} · {_e(travel)} · {_e(mine_row['dep'])}</div>"
                f"<div class='v'>{_e(mine_row['state'])}</div><div class='sub'>{_e(mine_row['detail'])}</div></div>")

    def ist(s):
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(tz).strftime("%d %b %Y %H:%M")
        except ValueError:
            return s or "never"
    units = runs[-1]["units_used"] if runs else "0"
    banner = '<div class="banner">SAMPLE DATA: generated with --mock, not real flights.</div>' if mock else ""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>BLR to DEL Tracker</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--fg:#1c1c1a;--mut:#6b6b66;--line:#e4e4df;--ok:#1a7f4b;--warn:#b26a00;--bad:#c62828;
--okbg:#e6f4ec;--warnbg:#fff3df;--badbg:#fde8e8;--mutbg:#eeeeea;--mine:#fffbe6}}
@media(prefers-color-scheme:dark){{:root{{--bg:#151514;--card:#1e1e1c;--fg:#ecece8;--mut:#9a9a93;--line:#34342f;
--ok:#5fcf93;--warn:#f0b04a;--bad:#ff7b7b;--okbg:#17301f;--warnbg:#38290f;--badbg:#3a1a1a;--mutbg:#2a2a27;--mine:#2f2b12}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:980px;margin:0 auto;padding:20px 16px 48px}}h1{{font-size:22px;margin:0 0 2px}}
h2{{font-size:17px;margin:28px 0 8px}}.meta,.sub{{color:var(--mut);font-size:12.5px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 14px}}
.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:560px}}
th,td{{text-align:left;padding:8px 8px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}}
th{{color:var(--mut);font-weight:600;font-size:12px}}tr:last-child td{{border-bottom:0}}tr.mine{{background:var(--mine)}}
.chip{{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600}}
.chip.ok{{color:var(--ok);background:var(--okbg)}}.chip.warn{{color:var(--warn);background:var(--warnbg)}}
.chip.bad{{color:var(--bad);background:var(--badbg)}}.chip.muted{{color:var(--mut);background:var(--mutbg)}}
.hero{{border-radius:12px;padding:14px 16px;margin:16px 0 0;border:1px solid var(--line)}}
.hero .v{{font-size:28px;font-weight:700}}.hero.ok{{background:var(--okbg);color:var(--ok)}}
.hero.warn{{background:var(--warnbg);color:var(--warn)}}.hero.bad{{background:var(--badbg);color:var(--bad)}}
.hero.muted{{background:var(--mutbg)}}.hero .k,.hero .sub{{color:inherit;opacity:.85}}
.banner{{background:var(--badbg);color:var(--bad);padding:8px 12px;border-radius:8px;margin-bottom:12px;font-weight:600}}
details{{margin-top:12px}}summary{{cursor:pointer;color:var(--mut);padding:4px 0}}
</style></head><body><main>{banner}
<h1>{_e(cfg['home_airport'])} → {_e(cfg['dest_airport'])} tracker</h1>
<div class="meta">Last option check: {_e(ist(last_run))} IST · API units used last run: {_e(units)}</div>
{hero}
<h2>1. {_e(cfg['home_airport'])} departures {_e(cfg['morning_window'][0])}–{_e(cfg['morning_window'][1])} (listed flight numbers only): delay log</h2>
<div class="card"><div class="scroll"><table><tr><th>Date</th><th>Flights</th><th>Avg delay (min)</th>
<th>Delayed ≥{thr} min</th><th>Worst (min)</th><th>Cancelled</th></tr>
{''.join(summ) or '<tr><td colspan=6 class="sub">No runs yet.</td></tr>'}</table></div>{detail}</div>
<h2>2. {_e(cfg['home_airport'])} → {_e(cfg['dest_airport'])} options on {_e(travel)}</h2>
<div class="card"><div class="scroll"><table><tr><th>Dep</th><th>Flight(s)</th><th>Status now</th><th>History</th></tr>
{opt_rows or '<tr><td colspan=4 class="sub">No runs yet.</td></tr>'}</table></div></div>
<p class="meta">Source: AeroDataBox free plan + SpiceJet schedule file. “NOT IN FEED” means the flight should operate
but is missing from the live airport board, which often precedes a cancellation but can also be a data gap.
Always confirm with the airline.</p>
</main></body></html>"""
    os.makedirs(docs_dir, exist_ok=True)
    with open(os.path.join(docs_dir, "index.html"), "w") as f:
        f.write(page)

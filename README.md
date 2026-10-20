# BLR → DEL flight tracker (free)

Runs twice a day on GitHub Actions (free), logs to CSV, and publishes a dashboard on GitHub Pages.

**Tracks**
1. **Every flight at BLR between 06:00 and 09:00** (departures *and* arrivals, all airlines, all routes): delay in minutes, status, daily summary (avg delay, % delayed, cancelled).
2. **Every BLR → DEL option on 28 Oct**: all direct flights from the live airport board (any airline), plus SpiceJet connecting itineraries from your schedule file, with each leg's live status. Your flight (SG141) is highlighted.

Alerts (optional, free): a push notification via ntfy.sh when any option becomes cancelled, goes missing from the feed, or when SG141 is delayed 15+ min.

## Setup (about 10 minutes)

1. **API key.** Create a free account at RapidAPI, subscribe to **AeroDataBox → Basic (free)**, copy your key.
2. **GitHub repo.** Create a **public** repo (Pages and unlimited Actions minutes are free for public repos) and upload everything in this folder, including the `.github` folder.
3. **Secrets.** Repo → Settings → Secrets and variables → Actions → add `RAPIDAPI_KEY`. Optional: `NTFY_TOPIC` = a hard-to-guess name (e.g. `bldel-x7k2q9`); install the ntfy app and subscribe to that topic.
4. **Pages.** Settings → Pages → Deploy from branch → `main` / `/docs`.
5. **First run.** Actions tab → *track-flights* → **Run workflow**. Then open `data/raw/` in the repo and the dashboard URL. If the dashboard shows real flights, you're done.

## Change things
Everything lives in `config.json` (travel date, your flight, window, delay threshold).
To preview the dashboard without a key: `python tracker.py --mock` (synthetic data, clearly labelled, written to `_mock/`).
To simulate a cancellation: `python tracker.py --mock --mock-cancel SG141`.
New SpiceJet schedule file: `python extract_schedule.py new.xlsx`.

## API budget (free plan ≈ 400 units/month, sources differ: 400–600)
Airport board and flight-by-number calls are 2 units each. Full run ≈ 14 units (1 morning call + 2 day-board calls + 4 leg-2 lookups, each 2 units), lean evening run = 4 units. About 18 units/day, so ~160 units for the 9 days to 28 Oct. A hard cap (`max_units_per_run`) stops runaway use, and units used are shown on the dashboard.

## Read this before trusting it
- **Not tested against the live API.** It was built against AeroDataBox's published response format and tested on synthetic data (16 unit tests). On the first run, check `data/raw/*.json` against the dashboard; if a field is shaped differently, `norm_flight()` in `tracker.py` is the one place to adjust.
- **Indian airport coverage can be patchy.** The API marks some data as "approximate"; delay figures depend on the airline/airport feeding live updates. A flight showing **NOT IN FEED** may be cancelled, or just a data gap.
- **Cancellations show up late.** Airlines usually update status a day or less before departure, so expect the real signal around 26–28 Oct. For SG141 (a route that starts 25 Oct) the feed may be empty until then; that is why it shows NOT IN FEED rather than "fine".
- Connecting itineraries cover **SpiceJet only** (that's what the file contains). Other airlines' direct flights are included via the live board.
- GitHub scheduled runs can start 10–30 minutes late. The data is for your information; always confirm with the airline (SMS/app) before acting.

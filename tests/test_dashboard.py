import csv
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import dashboard


CFG = {
    "home_airport": "BLR",
    "dest_airport": "DEL",
    "travel_date": "2026-10-28",
    "morning_window": ["00:00", "23:59"],
    "delay_threshold_min": 15,
    "morning_include_arrivals": False,
}


class Dashboard(unittest.TestCase):
    def test_render_hides_arrivals_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_dir = tmp_path / "data"
            docs_dir = tmp_path / "docs"
            data_dir.mkdir()

            with open(data_dir / "morning_delays.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=[
                    "run_utc", "date", "direction", "flight", "airline", "airport",
                    "sched", "revised", "delay_min", "status", "state",
                ])
                w.writeheader()
                w.writerows([
                    {"run_utc": "2026-10-20T07:26:22.300369Z", "date": "2026-10-20", "direction": "Arrival",
                     "flight": "SG650", "airline": "SpiceJet", "airport": "CCU", "sched": "02:10",
                     "revised": "05:36", "delay_min": "206", "status": "Arrived", "state": "DELAYED"},
                    {"run_utc": "2026-10-20T07:26:22.300369Z", "date": "2026-10-20", "direction": "Departure",
                     "flight": "SG645", "airline": "SpiceJet", "airport": "CCU", "sched": "05:20",
                     "revised": "06:45", "delay_min": "85", "status": "Departed", "state": "DELAYED"},
                ])

            with open(data_dir / "options.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["run_utc", "travel_date", "dep", "itinerary", "kind",
                                                  "airline", "via", "arr", "state", "delay_min", "detail", "mine"])
                w.writeheader()

            with open(data_dir / "runs.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["run_utc", "units_used"])
                w.writeheader()

            dashboard.render(CFG, str(data_dir), str(docs_dir), ZoneInfo("Asia/Kolkata"))
            html = (docs_dir / "index.html").read_text()

            self.assertIn(">Dep<", html)
            self.assertNotIn(">Arr<", html)
            self.assertIn("BLR departures 00:00–23:59", html)


if __name__ == "__main__":
    unittest.main()

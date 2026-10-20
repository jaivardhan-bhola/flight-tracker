import datetime as dt
import os
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tracker  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
D = dt.date(2026, 10, 28)
CFG = {"home_airport": "BLR", "dest_airport": "DEL", "my_flight": "SG141", "morning_window": ["06:00", "09:00"],
       "delay_threshold_min": 15, "morning_include_arrivals": True}
CL = tracker.make_classifier(15)


def fids(number, other, sched_local, revised_local=None, status="Expected", with_leg=True, airline="SpiceJet"):
    """AeroDataBox FIDS item in the documented shape (movement.airport = the OTHER airport)."""
    def t(x):
        d = dt.datetime.fromisoformat(x).replace(tzinfo=IST)
        return {"utc": d.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ"), "local": d.strftime("%Y-%m-%d %H:%M+05:30")}
    mv = {"airport": {"iata": other}, "scheduledTime": t(sched_local)}
    if revised_local:
        mv["revisedTime"] = t(revised_local)
    it = {"number": number, "status": status, "airline": {"name": airline}, "movement": mv}
    if with_leg:
        it["arrival"] = {"airport": {"iata": other}}
    return it


class Fake:
    """Client returning canned normalised flights."""
    def __init__(self, dep=(), arr=(), legs=None):
        self.dep = [tracker.norm_flight(i, home="BLR") for i in dep]
        self.arr = [tracker.norm_flight(i, home="BLR", direction="Arrival") for i in arr]
        self.legs = legs or {}
        self.units = 0

    def airport_departures(self, iata, start, end):
        return list(self.dep)

    def airport_window(self, iata, start, end):
        return list(self.dep) + list(self.arr)

    def flight_by_number(self, number, date_iso):
        return [tracker.norm_flight(i) for i in self.legs.get((number, date_iso), [])]


class Parsing(unittest.TestCase):
    def test_norm_departure_and_delay(self):
        f = tracker.norm_flight(fids("SG 141", "DEL", "2026-10-28 06:05", "2026-10-28 06:50"), home="BLR")
        self.assertEqual((f["number"], f["dest"], f["direction"]), ("SG141", "DEL", "Departure"))
        self.assertEqual(tracker.minutes(f["s_dep"], f["r_dep"]), 45)

    def test_dest_is_other_airport_even_without_leg(self):
        f = tracker.norm_flight(fids("6E 205", "BOM", "2026-10-28 06:10", with_leg=False), home="BLR")
        self.assertEqual(f["dest"], "BOM")

    def test_arrival_direction(self):
        f = tracker.norm_flight(fids("6E 7214", "DEL", "2026-10-28 06:20", "2026-10-28 06:35", with_leg=False),
                                home="BLR", direction="Arrival")
        self.assertEqual((f["origin"], f["dest"]), ("DEL", "BLR"))
        self.assertEqual(tracker.minutes(f["s_arr"], f["r_arr"]), 15)

    def test_utc_z_and_offset_formats(self):
        self.assertEqual(tracker.parse_ts("2026-10-28 00:35Z"), tracker.parse_ts("2026-10-28 06:05+05:30"))

    def test_classify(self):
        self.assertEqual(CL("Canceled", None), "CANCELLED")
        self.assertEqual(CL("CanceledUncertain", None), "CANCELLED?")
        self.assertEqual(CL("Expected", 20), "DELAYED")
        self.assertEqual(CL("Expected", 5), "ON TIME")
        self.assertEqual(CL("Expected", None), "SCHEDULED")
        self.assertEqual(CL("Delayed", None), "DELAYED")


class Schedule(unittest.TestCase):
    def setUp(self):
        self.sched = tracker.load_schedule()

    def ops(self, date, flights):
        return [r for r in self.sched if r["flights"] == flights and tracker.operates_on(r, date)]

    def test_my_flight_dates(self):
        self.assertTrue(self.ops(D, "SG141"))                          # 28 Oct (Wed)
        self.assertFalse(self.ops(dt.date(2026, 10, 24), "SG141"))     # before it starts

    def test_weekday_filter(self):
        self.assertTrue(self.ops(D, "SG722"))                          # Wed ok
        self.assertFalse(self.ops(dt.date(2026, 10, 31), "SG722"))     # Saturday excluded


class Morning(unittest.TestCase):
    def test_all_flights_both_directions(self):
        c = Fake(dep=[fids("SG 141", "DEL", "2026-10-28 06:05", "2026-10-28 06:35"),
                      fids("6E 531", "HYD", "2026-10-28 06:45"),
                      fids("AI 999", "BOM", "2026-10-28 09:40")],          # outside window -> dropped
                arr=[fids("6E 7214", "DEL", "2026-10-28 06:20", "2026-10-28 06:50", with_leg=False)])
        rows = tracker.morning(c, CFG, D, IST, CL)
        self.assertEqual([r["flight"] for r in rows], ["SG141", "6E7214", "6E531"])
        self.assertEqual(rows[0]["delay_min"], 30)
        self.assertEqual(rows[1]["direction"], "Arrival")


class Options(unittest.TestCase):
    def setUp(self):
        self.sched = tracker.load_schedule()

    def run_opts(self, client, with_legs=True):
        return {r["itinerary"]: r for r in tracker.options(client, CFG, self.sched, D, IST, CL, with_legs)}

    def test_my_flight_present_and_cancelled(self):
        c = Fake(dep=[fids("SG 141", "DEL", "2026-10-28 06:05", status="Canceled")])
        r = self.run_opts(c, with_legs=False)
        self.assertEqual(r["SG141"]["state"], "CANCELLED")
        self.assertEqual(r["SG141"]["mine"], "yes")

    def test_missing_from_feed(self):
        r = self.run_opts(Fake(dep=[]), with_legs=False)
        self.assertEqual(r["SG141"]["state"], "NOT IN FEED")     # schedule says it flies; feed lacks it
        self.assertEqual(r["SG722"]["state"], "NOT IN FEED")

    def test_other_airline_direct_included(self):
        c = Fake(dep=[fids("6E 6131", "DEL", "2026-10-28 06:30", airline="IndiGo")])
        self.assertIn("6E6131", self.run_opts(c, with_legs=False))

    def test_connecting_overnight_leg2_picks_next_day(self):
        # SG269 22:00 BLR->BOM, SG613 leaves BOM 21:35 -> must pick the 29 Oct instance
        leg2 = lambda d: [{"number": "SG613", "status": "Expected", "airline": {"name": "SpiceJet"},
                           "departure": {"airport": {"iata": "BOM"}, "scheduledTime": {
                               "utc": f"{d} 16:05Z", "local": f"{d} 21:35+05:30"}},
                           "arrival": {"airport": {"iata": "DEL"}}}]
        c = Fake(dep=[fids("SG 269", "BOM", "2026-10-28 22:00")],
                 legs={("SG613", "2026-10-28"): leg2("2026-10-28"), ("SG613", "2026-10-29"): leg2("2026-10-29")})
        r = self.run_opts(c)["SG269+SG613"]
        self.assertIn("(+1d)", r["detail"])

    def test_connecting_cancelled_leg_makes_itinerary_cancelled(self):
        c = Fake(dep=[fids("SG 640", "BOM", "2026-10-28 06:10", status="Canceled")])
        self.assertEqual(self.run_opts(c)["SG640+SG613"]["state"], "CANCELLED")


class Alerts(unittest.TestCase):
    def test_alert_only_on_change_into_bad_state(self):
        rows = [{"itinerary": "SG141", "dep": "06:05", "state": "CANCELLED", "mine": "yes", "delay_min": ""},
                {"itinerary": "SG722", "dep": "12:50", "state": "CANCELLED", "mine": "", "delay_min": ""}]
        lines, urgent = tracker.build_alerts(CFG, {"SG141": "SCHEDULED", "SG722": "CANCELLED"}, rows)
        self.assertEqual(len(lines), 1)
        self.assertTrue(urgent)
        self.assertEqual(tracker.build_alerts(CFG, {"SG141": "CANCELLED", "SG722": "CANCELLED"}, rows)[0], [])

    def test_my_flight_delay_alert(self):
        rows = [{"itinerary": "SG141", "dep": "06:05", "state": "DELAYED", "mine": "yes", "delay_min": "40"}]
        self.assertEqual(len(tracker.build_alerts(CFG, {"SG141": "SCHEDULED"}, rows)[0]), 1)


class Budget(unittest.TestCase):
    def test_budget_guard(self):
        c = tracker.AeroDataBox("k", budget=3)
        c.units = 2
        with self.assertRaises(tracker.BudgetExceeded):
            c.airport_departures("BLR", "2026-10-28T06:00", "2026-10-28T09:00")


if __name__ == "__main__":
    unittest.main()

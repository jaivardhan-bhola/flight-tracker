"""Synthetic stand-in for the AeroDataBox client, used by `tracker.py --mock` and the tests.

It emits payloads in the documented AeroDataBox shape (movement / departure / arrival with
{utc, local} times) and feeds them through the REAL parser (tracker.norm_flight), so the
parsing + classification + dashboard code paths are exercised without an API key.
Everything it returns is made up. Never mistake it for live data.
"""
import datetime as dt
import hashlib

import tracker

UTC = dt.timezone.utc
# (flight, airline, other airport, local HH:MM, direction)
FILLER = [
    ("6E 6131", "IndiGo", "DEL", "06:30", "Departure"), ("AI 2808", "Air India", "DEL", "07:15", "Departure"),
    ("QP 1402", "Akasa Air", "DEL", "08:05", "Departure"), ("6E 205", "IndiGo", "BOM", "06:10", "Departure"),
    ("6E 531", "IndiGo", "HYD", "06:45", "Departure"), ("AI 807", "Air India", "CCU", "07:40", "Departure"),
    ("UK 814", "Vistara", "BOM", "08:20", "Departure"), ("6E 6602", "IndiGo", "MAA", "08:45", "Departure"),
    ("QP 1101", "Akasa Air", "PNQ", "06:55", "Departure"), ("6E 6192", "IndiGo", "DEL", "13:10", "Departure"),
    ("AI 2510", "Air India", "DEL", "17:25", "Departure"),
    ("6E 7214", "IndiGo", "DEL", "06:20", "Arrival"), ("AI 502", "Air India", "BOM", "07:05", "Arrival"),
    ("QP 1209", "Akasa Air", "HYD", "07:50", "Arrival"), ("6E 441", "IndiGo", "CCU", "08:35", "Arrival"),
]
CITY = {"DEL": "Delhi", "BOM": "Mumbai", "HYD": "Hyderabad", "CCU": "Kolkata", "MAA": "Chennai",
        "PNQ": "Pune", "AMD": "Ahmedabad", "PAT": "Patna", "VNS": "Varanasi", "BLR": "Bengaluru"}


def _delay(number, date):
    h = int(hashlib.sha256(f"{number}{date}".encode()).hexdigest(), 16)
    return [0, 0, 0, 5, 10, 25, 45, 90][h % 8]


def _t(d, tz):
    return {"utc": d.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ"),
            "local": d.astimezone(tz).strftime("%Y-%m-%d %H:%M%z")[:-2] + ":" + d.astimezone(tz).strftime("%z")[-2:]}


class MockClient:
    def __init__(self, schedule, tz, cancel=()):
        self.schedule, self.tz, self.cancel = schedule, tz, set(cancel)
        self.units = 0
        self.now = dt.datetime.now(UTC)
        # leg-2 flight -> (origin iata, departs-local HH:MM) derived from the sheet's arrival time
        self.leg2 = {}
        for r in schedule:
            if r["kind"] == "connecting":
                n2 = r["flights"].split("|")[1]
                if n2 not in self.leg2:
                    arr = dt.datetime.strptime(r["arr"], "%I:%M %p") - dt.timedelta(minutes=140)
                    self.leg2[n2] = (r["via_iata"], arr.strftime("%H:%M"))

    # -- payload builders -------------------------------------------------------------
    def _status(self, number, sched, delay):
        if number in self.cancel:
            return "Canceled"
        if sched < self.now:
            return "Departed"
        return "Delayed" if delay >= 30 else "Expected"

    def _fids_item(self, number, airline, other, sched, direction, date):
        delay = _delay(number, date)
        status = self._status(number, sched, delay)
        item = {"number": number[:2] + " " + number[2:] if " " not in number else number,
                "status": status, "codeshareStatus": "IsOperator", "isCargo": False,
                "airline": {"name": airline}}
        mv = {"airport": {"iata": other, "name": CITY.get(other, other)},
              "scheduledTime": _t(sched, self.tz)}
        if status != "Canceled" and (delay or sched < self.now):
            mv["revisedTime"] = _t(sched + dt.timedelta(minutes=delay), self.tz)
        item["movement"] = mv
        if direction == "Departure":  # withLeg=true also gives the far end
            item["arrival"] = {"airport": {"iata": other}, "scheduledTime": _t(sched + dt.timedelta(minutes=170), self.tz)}
        return item

    def _day_items(self, date):
        out = []
        for r in self.schedule:
            if not tracker.operates_on(r, date) or r["kind"] == "connecting" and False:
                continue
            num = r["flights"].split("|")[0]
            other = r["via_iata"] or "DEL"
            sched = tracker.sheet_time(date, r["dep"], self.tz)
            out.append((num, "SpiceJet", other, sched, "Departure"))
        out = list({o[0]: o for o in out}.values())
        for num, al, other, hhmm, direction in FILLER:
            h, m = map(int, hhmm.split(":"))
            out.append((num.replace(" ", ""), al, other,
                        dt.datetime.combine(date, dt.time(h, m), tzinfo=self.tz), direction))
        return out

    def _window(self, iata, start, end, directions):
        s = dt.datetime.fromisoformat(start).replace(tzinfo=self.tz)
        e = dt.datetime.fromisoformat(end).replace(tzinfo=self.tz)
        res = {"departures": [], "arrivals": []}
        for num, al, other, sched, direction in self._day_items(s.date()):
            if direction in directions and s <= sched <= e:
                res["departures" if direction == "Departure" else "arrivals"].append(
                    self._fids_item(num, al, other, sched, direction, sched.date()))
        self.units += 2
        return res

    # -- same interface as tracker.AeroDataBox ---------------------------------------
    def airport_departures(self, iata, start, end):
        d = self._window(iata, start, end, {"Departure"})
        return [tracker.norm_flight(i, home=iata) for i in d["departures"]]

    def airport_window(self, iata, start, end):
        d = self._window(iata, start, end, {"Departure", "Arrival"})
        return ([tracker.norm_flight(i, home=iata) for i in d["departures"]] +
                [tracker.norm_flight(i, home=iata, direction="Arrival") for i in d["arrivals"]])

    def flight_by_number(self, number, date_iso):
        self.units += 2
        if number not in self.leg2:
            return []
        origin, hhmm = self.leg2[number]
        h, m = map(int, hhmm.split(":"))
        date = dt.date.fromisoformat(date_iso)
        sched = dt.datetime.combine(date, dt.time(h, m), tzinfo=self.tz)
        delay = _delay(number, date)
        status = self._status(number, sched, delay)
        item = {"number": number, "status": status, "airline": {"name": "SpiceJet"},
                "departure": {"airport": {"iata": origin}, "scheduledTime": _t(sched, self.tz)},
                "arrival": {"airport": {"iata": "DEL"},
                            "scheduledTime": _t(sched + dt.timedelta(minutes=140), self.tz)}}
        if status != "Canceled" and delay:
            item["departure"]["revisedTime"] = _t(sched + dt.timedelta(minutes=delay), self.tz)
        return [tracker.norm_flight(item)]

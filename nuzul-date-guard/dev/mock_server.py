#!/usr/bin/env python3
"""
Offline stand-in for the restful-booker staging API.

It copies the behaviour observed on staging on 2026-11-09, including the bugs,
so the guard can be demonstrated and developed when staging is asleep:
  - no date validation on POST, PUT or PATCH
  - dates parsed loosely and re-formatted (2026-02-30 -> 2026-03-02,
    12/08/2026 -> 2026-12-08, anything unparseable -> 0NaN-aN-aN)
  - missing bookingdates -> 500
  - PATCH/PUT on an unknown id -> 405, GET on an unknown id -> 404

Run:  python3 dev/mock_server.py            (listens on http://localhost:3001)
Then: python3 guard.py --base-url http://localhost:3001

Pass --fixed to emulate an API that validates dates, to see the guard go green.
"""

import json
import re
import secrets
import sys
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

FIXED = "--fixed" in sys.argv
BOOKINGS = {}
TOKENS = set()
NEXT_ID = [1]


def js_like_date(value):
    """Mimic how the real API turns a string into YYYY-MM-DD."""
    if not isinstance(value, str):
        return "0NaN-aN-aN"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", value)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", value)  # US month/day/year
        if not m:
            return "0NaN-aN-aN"
        mo, d, y = map(int, m.groups())
    if not 1 <= mo <= 12 or not 1 <= d <= 31:
        return "0NaN-aN-aN"
    return (date(y, mo, 1) + timedelta(days=d - 1)).isoformat()  # rolls over like JS


def strict_problem(dates):
    """Used only with --fixed: what a correct API would refuse."""
    if not isinstance(dates, dict):
        return "bookingdates is required"
    try:
        cin = date.fromisoformat(dates.get("checkin", ""))
        cout = date.fromisoformat(dates.get("checkout", ""))
    except (TypeError, ValueError):
        return "dates must be real YYYY-MM-DD dates"
    if cout <= cin:
        return "checkout must be after checkin"
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body):
        payload = body if isinstance(body, str) else json.dumps(body)
        ctype = "text/plain" if isinstance(body, str) else "application/json"
        data = payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def authorised(self):
        cookie = self.headers.get("Cookie") or ""
        return any(cookie == "token=" + t for t in TOKENS)

    def booking_id(self):
        m = re.match(r"^/booking/(\d+)$", self.path)
        return int(m.group(1)) if m else None

    def clean(self, dates):
        return {"checkin": js_like_date(dates.get("checkin")),
                "checkout": js_like_date(dates.get("checkout"))}

    def do_GET(self):
        if self.path == "/ping":
            return self.send(201, "Created")
        if self.path == "/booking":
            return self.send(200, [{"bookingid": i} for i in BOOKINGS])
        bid = self.booking_id()
        if bid in BOOKINGS:
            return self.send(200, BOOKINGS[bid])
        return self.send(404, "Not Found")

    def do_POST(self):
        if self.path == "/auth":
            b = self.body()
            if b.get("username") == "admin" and b.get("password") == "password123":
                tok = secrets.token_hex(8)[:15]
                TOKENS.add(tok)
                return self.send(200, {"token": tok})
            return self.send(200, {"reason": "Bad credentials"})
        if self.path == "/booking":
            b = self.body()
            if FIXED:
                problem = strict_problem(b.get("bookingdates"))
                if problem:
                    return self.send(400, {"error": problem})
            if "bookingdates" not in b:
                return self.send(500, "Internal Server Error")
            b["bookingdates"] = self.clean(b["bookingdates"])
            bid = NEXT_ID[0]
            NEXT_ID[0] += 1
            BOOKINGS[bid] = b
            return self.send(200, {"bookingid": bid, "booking": b})
        return self.send(404, "Not Found")

    def update(self, partial):
        bid = self.booking_id()
        if not self.authorised():
            return self.send(403, "Forbidden")
        if bid not in BOOKINGS:
            return self.send(405, "Method Not Allowed")
        b = self.body()
        if FIXED and "bookingdates" in b:
            problem = strict_problem(b["bookingdates"])
            if problem:
                return self.send(400, {"error": problem})
        if "bookingdates" in b:
            b["bookingdates"] = self.clean(b["bookingdates"])
        BOOKINGS[bid] = {**BOOKINGS[bid], **b} if partial else b
        return self.send(200, BOOKINGS[bid])

    def do_PATCH(self):
        return self.update(partial=True)

    def do_PUT(self):
        return self.update(partial=False)

    def do_DELETE(self):
        bid = self.booking_id()
        if not self.authorised():
            return self.send(403, "Forbidden")
        BOOKINGS.pop(bid, None)
        return self.send(201, "Created")


if __name__ == "__main__":
    port = 3001
    mode = "FIXED (validates dates)" if FIXED else "BUGGY (matches staging)"
    print("Mock booking API on http://localhost:{}  ·  {}".format(port, mode))
    HTTPServer(("localhost", port), Handler).serve_forever()

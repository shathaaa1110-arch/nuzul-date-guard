#!/usr/bin/env python3
"""
Booking Date Guard

Checks that the Nuzul booking API refuses bad stay dates on every path that
writes data (create, PATCH, PUT), and reports what was actually stored when
it does not.

Two modes:
  python3 guard.py probe   run the cases in cases.json (default)
  python3 guard.py audit   scan bookings already stored and flag bad dates

Standard library only. Python 3.8+.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

DEFAULT_BASE_URL = "https://restful-booker.herokuapp.com"
ADMIN_USER = os.environ.get("BOOKER_USER", "admin")
ADMIN_PASS = os.environ.get("BOOKER_PASS", "password123")

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def color(text, code):
    return "\033[{}m{}\033[0m".format(code, text) if USE_COLOR else text


def green(t):
    return color(t, "32")


def red(t):
    return color(t, "31")


def yellow(t):
    return color(t, "33")


def dim(t):
    return color(t, "2")


# --------------------------------------------------------------------------
# Date rules: the checks NZL-217 asks for, applied to whatever was stored
# --------------------------------------------------------------------------

def parse_iso(value):
    """Return a date if value is a real YYYY-MM-DD date, else None."""
    if not isinstance(value, str) or not ISO_DATE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def date_problems(checkin, checkout):
    """List what is wrong with a pair of stored dates. Empty list = fine."""
    problems = []
    cin, cout = parse_iso(checkin), parse_iso(checkout)
    if cin is None:
        problems.append("checkin {!r} is not a real YYYY-MM-DD date".format(checkin))
    if cout is None:
        problems.append("checkout {!r} is not a real YYYY-MM-DD date".format(checkout))
    if cin and cout and cout <= cin:
        nights = (cout - cin).days
        problems.append("checkout is not after checkin ({} nights)".format(nights))
    return problems


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------

class Response:
    def __init__(self, status, text):
        self.status = status
        self.text = text
        try:
            self.json = json.loads(text) if text else None
        except ValueError:
            self.json = None


class BookerApi:
    def __init__(self, base_url, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def call(self, method, path, body=None, token=None):
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Cookie"] = "token=" + token
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return Response(resp.status, resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as err:
            return Response(err.code, err.read().decode("utf-8", "replace"))

    def wait_until_up(self, attempts=6, delay=5):
        """Staging sleeps when idle; keep pinging until it answers."""
        for attempt in range(1, attempts + 1):
            try:
                if self.call("GET", "/ping").status in (200, 201):
                    return True
            except (urllib.error.URLError, OSError):
                pass
            if attempt < attempts:
                print(dim("  API not answering yet, retrying in {}s ({}/{})".format(
                    delay, attempt, attempts)))
                time.sleep(delay)
        return False

    def token(self):
        resp = self.call("POST", "/auth", {"username": ADMIN_USER, "password": ADMIN_PASS})
        tok = (resp.json or {}).get("token")
        if not tok:
            raise RuntimeError("could not get a token: {} {}".format(resp.status, resp.text))
        return tok

    def create(self, dates):
        body = {
            "firstname": "DateGuard",
            "lastname": "QA",
            "totalprice": 480,
            "depositpaid": True,
        }
        if dates is not None:
            body["bookingdates"] = dates
        return self.call("POST", "/booking", body)

    def read_dates(self, booking_id):
        resp = self.call("GET", "/booking/{}".format(booking_id))
        if resp.status != 200 or not resp.json:
            return None
        return resp.json.get("bookingdates")


# --------------------------------------------------------------------------
# Running one case
# --------------------------------------------------------------------------

class Result:
    def __init__(self, case):
        self.case = case
        self.passed = False
        self.status = None
        self.sent = case.get("dates")
        self.stored = None
        self.notes = []
        self.booking_id = None

    def as_dict(self):
        return {
            "id": self.case["id"],
            "title": self.case["title"],
            "severity": self.case.get("severity"),
            "expect": self.case["expect"],
            "verdict": "PASS" if self.passed else "FAIL",
            "status": self.status,
            "sent": self.sent,
            "stored": self.stored,
            "booking_id": self.booking_id,
            "notes": self.notes,
        }


def describe_storage(result):
    """Add notes about what the API kept, including silent changes."""
    if result.stored is None:
        return
    sent = result.sent or {}
    for field in ("checkin", "checkout"):
        if field in sent and result.stored.get(field) != sent[field]:
            result.notes.append("{} silently changed: sent {!r}, stored {!r}".format(
                field, sent[field], result.stored.get(field)))
    for problem in date_problems(result.stored.get("checkin"), result.stored.get("checkout")):
        result.notes.append("stored data invalid: " + problem)


def run_case(api, case, valid_dates, created):
    result = Result(case)
    kind, expect = case["kind"], case["expect"]

    if kind == "create":
        resp = api.create(case.get("dates"))
        result.status = resp.status
        booking_id = (resp.json or {}).get("bookingid")
        if booking_id:
            created.append(booking_id)
            result.booking_id = booking_id
            result.stored = api.read_dates(booking_id)

    elif kind in ("patch", "put", "patch_no_token"):
        setup = api.create(valid_dates)
        booking_id = (setup.json or {}).get("bookingid")
        if not booking_id:
            result.notes.append("setup failed: could not create a valid booking ({})".format(
                setup.status))
            return result
        created.append(booking_id)
        result.booking_id = booking_id
        token = None if kind == "patch_no_token" else api.token()
        if kind == "put":
            body = {"firstname": "DateGuard", "lastname": "QA", "totalprice": 480,
                    "depositpaid": True, "bookingdates": case["dates"]}
            resp = api.call("PUT", "/booking/{}".format(booking_id), body, token)
        else:
            body = {"bookingdates": case["dates"]}
            resp = api.call("PATCH", "/booking/{}".format(booking_id), body, token)
        result.status = resp.status
        result.stored = api.read_dates(booking_id)
        if result.stored is None:
            result.notes.append("booking {} could not be read back".format(booking_id))
    else:
        result.notes.append("unknown kind {!r} in cases.json".format(kind))
        return result

    # ---- verdicts -------------------------------------------------------
    if expect == "accept":
        describe_storage(result)
        result.passed = result.status == 200 and not result.notes
        if result.status != 200:
            result.notes.insert(0, "expected 200, got {}".format(result.status))

    elif expect == "reject":
        if result.status is not None and result.status >= 500:
            result.notes.append("server error {} instead of a 400 with a reason".format(
                result.status))
        elif result.status is not None and not 400 <= result.status < 500:
            result.notes.append("accepted with {}; expected 400".format(result.status))
        if kind in ("patch", "put"):
            changed = result.stored != valid_dates
            if changed:
                result.notes.append("booking changed from {} to {}".format(
                    fmt_dates(valid_dates), fmt_dates(result.stored)))
                describe_storage(result)
            result.passed = 400 <= (result.status or 0) < 500 and not changed
        else:
            if result.booking_id:
                describe_storage(result)
            result.passed = 400 <= (result.status or 0) < 500 and not result.booking_id

    elif expect == "forbidden":
        changed = result.stored != valid_dates
        if result.status != 403:
            result.notes.append("expected 403, got {}".format(result.status))
        if changed:
            result.notes.append("booking changed without a token")
        result.passed = result.status == 403 and not changed

    return result


def fmt_dates(d):
    if not d:
        return "(none)"

    def show(v):
        return '""' if v == "" else str(v)

    return "{} -> {}".format(show(d.get("checkin")), show(d.get("checkout")))


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def print_result(r):
    tag = green("PASS") if r.passed else red("FAIL")
    sev = r.case.get("severity", "")
    print("{}  {}  {:<62} {}".format(tag, r.case["id"], r.case["title"][:62], dim(sev)))
    status = "status {}".format(r.status) if r.status is not None else "no response"
    line = "       {}  sent {}".format(status, fmt_dates(r.sent) if r.sent else "(no dates)")
    if r.stored is not None:
        line += "  stored {}".format(fmt_dates(r.stored))
    print(dim(line))
    for note in r.notes:
        print("       " + (yellow(note) if r.passed else red(note)))


def write_markdown(path, base_url, results, started):
    lines = [
        "# Booking Date Guard report",
        "",
        "- API: `{}`".format(base_url),
        "- Run at: {} UTC".format(started.strftime("%Y-%m-%d %H:%M:%S")),
        "- Result: {} passed, {} failed".format(
            sum(r.passed for r in results), sum(not r.passed for r in results)),
        "",
        "| ID | Check | Severity | Verdict | Status | Sent | Stored |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            r.case["id"], r.case["title"], r.case.get("severity", ""),
            "PASS" if r.passed else "**FAIL**", r.status,
            fmt_dates(r.sent) if r.sent else "(no dates)",
            fmt_dates(r.stored) if r.stored is not None else "-"))
    failed = [r for r in results if not r.passed]
    if failed:
        lines += ["", "## Failures", ""]
        for r in failed:
            lines.append("### {} {}".format(r.case["id"], r.case["title"]))
            if r.case.get("why"):
                lines.append("_Why it matters:_ " + r.case["why"])
            if r.booking_id:
                lines.append("- Booking id: {}".format(r.booking_id))
            for note in r.notes:
                lines.append("- " + note)
            lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def probe(api, args):
    with open(args.cases, encoding="utf-8") as fh:
        spec = json.load(fh)
    valid_dates = spec["valid_dates"]
    cases = spec["cases"]
    if args.only:
        wanted = {c.strip() for c in args.only.split(",")}
        cases = [c for c in cases if c["id"] in wanted]

    started = datetime.now(timezone.utc)
    print("Booking Date Guard  ·  {}  ·  {} UTC".format(
        api.base_url, started.strftime("%Y-%m-%d %H:%M")))
    print("-" * 78)

    results, created = [], []
    for case in cases:
        try:
            result = run_case(api, case, valid_dates, created)
        except Exception as exc:  # keep going; one broken case should not hide the rest
            result = Result(case)
            result.notes.append("error while running case: {}".format(exc))
        results.append(result)
        print_result(result)

    print("-" * 78)
    passed = sum(r.passed for r in results)
    failed = len(results) - passed
    print("{} passed  ·  {} failed".format(green(str(passed)), red(str(failed)) if failed else "0"))
    real_failures = [r for r in results if not r.passed and r.case.get("severity") != "control"]
    control_failures = [r for r in results if not r.passed and r.case.get("severity") == "control"]
    if control_failures:
        print(yellow("Control checks failed: fix the setup before trusting other results."))
    elif real_failures:
        print(red("Date validation is NOT in place (NZL-217 not satisfied)."))
    else:
        print(green("All date checks passed."))

    if args.report:
        write_markdown(args.report, api.base_url, results, started)
        json_path = os.path.splitext(args.report)[0] + ".json"
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump([r.as_dict() for r in results], fh, indent=2, ensure_ascii=False)
        print(dim("Report written to {} and {}".format(args.report, json_path)))

    if created and not args.keep:
        cleanup(api, created)
    return 1 if failed else 0


def cleanup(api, booking_ids):
    try:
        token = api.token()
    except RuntimeError:
        return
    for booking_id in booking_ids:
        api.call("DELETE", "/booking/{}".format(booking_id), token=token)


def audit(api, args):
    """Scan stored bookings for dates that should never have been accepted."""
    resp = api.call("GET", "/booking")
    ids = [item["bookingid"] for item in (resp.json or []) if "bookingid" in item]
    ids = ids[: args.limit]
    print("Auditing {} stored bookings on {}".format(len(ids), api.base_url))
    print("-" * 78)
    bad = 0
    for booking_id in ids:
        dates = api.read_dates(booking_id)
        if dates is None:
            continue
        problems = date_problems(dates.get("checkin"), dates.get("checkout"))
        if problems:
            bad += 1
            print("{}  booking {:<6} {}".format(red("BAD "), booking_id, fmt_dates(dates)))
            for p in problems:
                print("       " + red(p))
    print("-" * 78)
    print("{} of {} bookings have invalid dates".format(bad, len(ids)))
    return 1 if bad else 0


def main():
    parser = argparse.ArgumentParser(description="Check that bad booking dates are refused.")
    parser.add_argument("mode", nargs="?", default="probe", choices=["probe", "audit"])
    parser.add_argument("--base-url", default=os.environ.get("BOOKER_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--cases", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.json"))
    parser.add_argument("--only", help="comma-separated case ids, e.g. DG-02,DG-09")
    parser.add_argument("--report", default="report.md", help="markdown report path ('' to skip)")
    parser.add_argument("--limit", type=int, default=30, help="audit: max bookings to check")
    parser.add_argument("--keep", action="store_true", help="probe: keep the bookings it created")
    args = parser.parse_args()

    api = BookerApi(args.base_url)
    if not api.wait_until_up():
        print(red("API at {} is not reachable.".format(args.base_url)))
        return 2
    return probe(api, args) if args.mode == "probe" else audit(api, args)


if __name__ == "__main__":
    sys.exit(main())

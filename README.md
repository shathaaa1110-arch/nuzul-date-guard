# Booking Date Guard

A small command-line check that the Nuzul booking API refuses bad stay dates
on every path that writes data, and shows exactly what it stored when it
doesn't.

```
$ python3 guard.py

Booking Date Guard  ·  https://restful-booker.herokuapp.com  ·  2026-11-09 17:37 UTC
------------------------------------------------------------------------------
PASS  DG-01  Valid booking is accepted and stored exactly as sent           control
FAIL  DG-02  Checkout before checkin is rejected on create                  critical
       status 200  sent 2026-08-15 -> 2026-08-12  stored 2026-08-15 -> 2026-08-12
       accepted with 200; expected 400
       stored data invalid: checkout is not after checkin (-3 nights)
FAIL  DG-05  Day/month string 12/08/2026 is rejected, not reinterpreted     critical
       status 200  sent 2026-08-15 -> 12/08/2026  stored 2026-08-15 -> 2026-12-08
       checkout silently changed: sent '12/08/2026', stored '2026-12-08'
...
2 passed  ·  9 failed
Date validation is NOT in place (NZL-217 not satisfied).
```

## Why this tool

Guests at Nuzul were arriving on dates that didn't match the system, and
nobody could say whether it came from the phone agent, the website or
reception. All three write through one booking API, so that is where a bad
date either gets stopped or gets in. Checking the API covers every channel
at once.

When I tested Release 14 by hand, the API accepted reversed dates on create,
PATCH and PUT, and it silently rewrote some dates (30 Feb became 2 Mar,
12/08/2026 became 8 December). The second kind is the dangerous one: the
stored value looks valid, so nothing downstream notices. A check that only
looks at the status code would miss it. This tool reads every booking back
after writing it and compares what was stored with what was sent.

It turns ticket NZL-217 into something that runs before every release, so
the next "fix" can't ship without proving it works.

## Run it

Needs Python 3.8 or newer. No packages to install.

```
git clone https://github.com/shathaaa1110-arch/nuzul-date-guard.git
cd nuzul-date-guard
python3 guard.py
```

The first request can take up to 30 seconds while staging wakes up; the tool
retries on its own.

| Command | What it does |
|---|---|
| `python3 guard.py` | Run every case in `cases.json` against staging |
| `python3 guard.py --only DG-02,DG-09` | Run selected cases |
| `python3 guard.py audit` | Scan bookings already stored and list any with invalid dates |
| `python3 guard.py --base-url URL` | Point at another environment (or set `BOOKER_BASE_URL`) |
| `python3 guard.py --keep` | Keep the test bookings instead of deleting them |
| `python3 guard.py --report ""` | Skip writing report files |

Each run writes `report.md` (readable summary with failures explained) and
`report.json` (for other tools).

**Exit codes:** `0` all checks passed · `1` at least one check failed ·
`2` API not reachable. A CI pipeline can block a release on a non-zero exit.

## What it checks

| Case | Check | Why |
|---|---|---|
| DG-01 | A valid booking is accepted and stored as sent | Control: proves the setup works |
| DG-02 | Checkout before checkin is rejected | The BK-1043 case |
| DG-03 | Zero-night stay is rejected | Assumption, to confirm with the team |
| DG-04 | 30 February is rejected, not rolled over | Silent change to a real-looking date |
| DG-05 | `12/08/2026` is rejected, not reinterpreted | Day/month vs month/day between channels |
| DG-06 | Non-date text is rejected | Garbage must not be stored |
| DG-07 | Empty checkout is rejected | What an agent sends when it missed the date |
| DG-08 | Missing dates give a 400, not a 500 | A crash tells the caller nothing |
| DG-09 | PATCH can't move checkout before checkin | The path used when a caller changes dates |
| DG-10 | PUT can't store reversed dates | Second update path |
| DG-11 | PATCH without a token is refused | Control: proves the API can reject |

A case **fails** if the API accepts the request, crashes, stores something
different from what was sent, or stores dates that break the rules
(not a real `YYYY-MM-DD` date, or checkout not after checkin).

If a **control** case fails, the tool says so: the other results can't be
trusted until the setup is fixed.

### How it stays reliable on staging

Staging sleeps when idle and wipes its data every ten minutes, so the tool
never depends on existing data. Each case creates the booking it needs, gets
a fresh token, does its check, reads the booking back within seconds, then
deletes what it created.

## Add a case

Add an entry to `cases.json`. No code changes.

```json
{
  "id": "DG-12",
  "title": "Checkin in the past is rejected",
  "kind": "create",
  "dates": { "checkin": "2020-01-01", "checkout": "2020-01-03" },
  "expect": "reject",
  "severity": "medium",
  "why": "Shown in the report next to a failure."
}
```

- `kind`: `create`, `patch`, `put` or `patch_no_token`. Update kinds first
  create a valid booking using `valid_dates`, then try to change it.
- `expect`: `accept`, `reject` or `forbidden`.
- `dates`: the `bookingdates` to send; `null` sends none.

## Offline demo

`dev/mock_server.py` imitates the staging API, bugs included, for when
staging is down.

```
python3 dev/mock_server.py            # terminal 1: behaves like staging today
python3 guard.py --base-url http://localhost:3001

python3 dev/mock_server.py --fixed    # an API that validates dates: all green
```

## Limits and next steps

- `audit` flags dates that are impossible, but it can't see a date that was
  silently reinterpreted into a valid one (12/08 stored as 8 December looks
  fine on its own). Only `probe`, which knows what was sent, catches that.
- It tests the API, not the console or the voice agent. The natural next step
  is a transcript check: take a voice-agent call and its booking, and flag
  where the confirmed dates, name, price or deposit don't match the record
  (the BK-1043 call fails on all four).
- Rules like "is a same-day stay allowed?" are assumptions until the team
  confirms them; they live in `cases.json` so they're easy to change.

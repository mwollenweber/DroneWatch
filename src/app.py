#!/usr/bin/env python3
"""
AWS Lambda: poll the public NOPD drone flight layer and tweet each new flight.

Flights are uploaded in batches days after they fly, and ObjectIds are not in
takeoff order, so "new" means "a flight_id not seen before", not "took off
since the last poll". Each run fetches every flight (attributes only, ~150 KB),
diffs against a DynamoDB table of flights already handled, and tweets the new
ones oldest first.

  - Empty table (first run): record every existing flight, tweet nothing.
  - More than FLOOD_THRESHOLD new at once: tweet nothing, log an error. That
    usually means the layer was rebuilt; invoke with {"seed": true} to accept.
  - At most MAX_TWEETS_PER_RUN per run; the rest wait for the next run.
  - A flight is recorded only after its tweet succeeds, so failures retry.

Event overrides: {"dry_run": true} logs tweets without posting them;
{"seed": true} records everything as seen without tweeting.

# preview tweet text for the 5 latest flights; needs no AWS or X credentials
python app.py --preview 5

Copyright 2026 mjw@insomniac.tech
"""

import argparse, json, logging, math, os
from datetime import datetime, timedelta, timezone

import requests
from requests_oauthlib import OAuth1

from dump import BASE, LOCAL, fetch_pages

log = logging.getLogger()
log.setLevel(logging.INFO)

TWEET_URL = "https://api.x.com/2/tweets"
EPOCH = datetime(2000, 1, 1)
DESC_MAX = 80


class RateLimited(Exception):
    pass


class Duplicate(Exception):
    pass


def fetch_flights(session):
    """Every flight on the layer, as attribute dicts."""
    end = datetime.now(LOCAL).replace(tzinfo=None) + timedelta(days=2)
    return [f["attributes"] for pg in fetch_pages(EPOCH, end, session=session)
            for f in pg.get("features", [])]


def flight_center(object_id, session):
    """(lat, lon) at the middle of a flight's bounding box, or None.

    A flight path can be megabytes, so ask the server for the extent only.
    """
    try:
        r = session.get(BASE, params={"objectIds": object_id,
                                      "returnExtentOnly": "true", "f": "json"},
                        timeout=30)
        r.raise_for_status()
        e = r.json()["extent"]
        lat, lon = (e["ymin"] + e["ymax"]) / 2, (e["xmin"] + e["xmax"]) / 2
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        log.warning("no extent for ObjectId %s: %s", object_id, exc)
        return None
    return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) else None


def clock(t):
    return t.strftime("%I:%M %p").lstrip("0")


def compose(a, center=None):
    """Tweet text for one flight's attributes."""
    t0 = datetime.fromtimestamp(a["takeoff"] / 1000, LOCAL)
    lines = [f"NOPD drone flight: {a.get('flight_purpose') or 'purpose not given'}"]

    desc = (a.get("description") or "").strip()
    if desc:
        if len(desc) > DESC_MAX:
            desc = desc[:DESC_MAX - 1].rstrip() + "…"
        lines.append(f"“{desc}”")

    when = f"{t0:%a %b} {t0.day}, {t0:%Y}, "
    if a.get("landing"):
        t1 = datetime.fromtimestamp(a["landing"] / 1000, LOCAL)
        start, end = clock(t0), clock(t1)
        if start[-2:] == end[-2:]:
            start = start[:-3]
        mins = round((a["landing"] - a["takeoff"]) / 60000)
        when += f"{start}–{end} ({mins} min)" if mins else f"{start}–{end} (<1 min)"
    else:
        when += clock(t0)
    lines.append(when)

    if a.get("external_id"):
        lines.append(f"Item #{a['external_id']}")
    if center:
        lines.append("https://www.google.com/maps/search/?api=1&query="
                     f"{center[0]:.5f},{center[1]:.5f}")
    return "\n".join(lines)


class Store:
    """DynamoDB table of flights already handled, keyed by flight_id."""

    def __init__(self, table_name):
        import boto3
        self.table = boto3.resource("dynamodb").Table(table_name)

    def seen(self):
        ids, kw = set(), {"ProjectionExpression": "flight_id"}
        while True:
            page = self.table.scan(**kw)
            ids.update(i["flight_id"] for i in page["Items"])
            if "LastEvaluatedKey" not in page:
                return ids
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def record(self, a, status, tweet_id=None):
        self.table.put_item(Item=_item(a, status, tweet_id))

    def record_all(self, flights, status):
        with self.table.batch_writer(overwrite_by_pkeys=["flight_id"]) as b:
            for a in flights:
                b.put_item(Item=_item(a, status))


def _item(a, status, tweet_id=None):
    item = {"flight_id": a["flight_id"], "object_id": a["ObjectId"],
            "takeoff": a["takeoff"], "status": status,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if tweet_id:
        item["tweet_id"] = tweet_id
    return item


class X:
    """Posts tweets with OAuth 1.0a user credentials from Secrets Manager."""

    def __init__(self, secret_id, session):
        self.secret_id, self.session, self._auth = secret_id, session, None

    def auth(self):
        # loaded lazily so seeding and dry runs work before the secret is filled
        if self._auth is None:
            import boto3
            c = json.loads(boto3.client("secretsmanager").get_secret_value(
                SecretId=self.secret_id)["SecretString"])
            self._auth = OAuth1(c["api_key"], c["api_secret"],
                                c["access_token"], c["access_token_secret"])
        return self._auth

    def post(self, text):
        r = self.session.post(TWEET_URL, json={"text": text}, auth=self.auth(),
                              timeout=30)
        if r.status_code == 429:
            raise RateLimited(f"resets at {r.headers.get('x-rate-limit-reset')}")
        if r.status_code == 403 and "duplicate" in r.text.lower():
            raise Duplicate(r.text[:300])
        if r.status_code >= 400:
            raise RuntimeError(f"X API {r.status_code}: {r.text[:500]}")
        return r.json()["data"]["id"]


def run(store, post, session, *, seed=False, dry_run=False,
        max_tweets=10, flood_threshold=50):
    flights = fetch_flights(session)
    seen = store.seen()
    new = sorted((a for a in flights if a["flight_id"] not in seen),
                 key=lambda a: (a["takeoff"], a["ObjectId"]))
    summary = {"flights": len(flights), "seen": len(seen), "new": len(new),
               "tweeted": 0, "failed": 0}

    if seed or not seen:
        store.record_all(new, "seeded")
        log.info("seeded %d flights without tweeting", len(new))
        return {**summary, "seeded": len(new)}

    if len(new) > flood_threshold:
        log.error('%d new flights exceeds FLOOD_THRESHOLD=%d; not tweeting. '
                  'Invoke with {"seed": true} to record them as seen.',
                  len(new), flood_threshold)
        return {**summary, "flood": True}

    for i, a in enumerate(new[:max_tweets]):
        text = compose(a, flight_center(a["ObjectId"], session))
        if dry_run:
            log.info("DRY RUN, would tweet %s:\n%s", a["flight_id"], text)
            continue
        try:
            tweet_id = post(text)
        except RateLimited as exc:
            log.warning("rate limited (%s); %d flights left for the next run",
                        exc, len(new) - i)
            break
        except Duplicate as exc:
            # already posted, e.g. the table write failed after a previous tweet
            log.warning("X says %s is a duplicate, recording it: %s",
                        a["flight_id"], exc)
            store.record(a, "duplicate")
            continue
        except Exception:
            log.exception("tweet failed for %s; will retry next run", a["flight_id"])
            summary["failed"] += 1
            continue
        store.record(a, "tweeted", tweet_id)
        summary["tweeted"] += 1
        log.info("tweeted %s as %s", a["flight_id"], tweet_id)

    if len(new) > max_tweets:
        log.info("%d new flights over MAX_TWEETS_PER_RUN wait for the next run",
                 len(new) - max_tweets)
    log.info("summary %s", json.dumps(summary))
    if summary["failed"]:
        # surface in the Lambda Errors metric; the flights retry next run
        raise RuntimeError(f"{summary['failed']} tweet(s) failed: {summary}")
    return summary


def handler(event, context):
    event = event if isinstance(event, dict) else {}
    session = requests.Session()
    return run(Store(os.environ["TABLE_NAME"]),
               X(os.environ["SECRET_ID"], session).post, session,
               seed=bool(event.get("seed")),
               dry_run=bool(event.get("dry_run"))
                       or os.environ.get("DRY_RUN", "").lower() == "true",
               max_tweets=int(os.environ.get("MAX_TWEETS_PER_RUN", 10)),
               flood_threshold=int(os.environ.get("FLOOD_THRESHOLD", 50)))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--preview", type=int, metavar="N", required=True,
                   help="print tweet text for the N latest flights")
    args = p.parse_args()

    session = requests.Session()
    latest = sorted(fetch_flights(session), key=lambda a: -a["takeoff"])
    for a in latest[:args.preview]:
        print(compose(a, flight_center(a["ObjectId"], session)), end="\n\n")


if __name__ == "__main__":
    main()

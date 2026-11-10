# NOPD drone flight tweeter

An AWS Lambda that polls NOPD's public Skydio/ArcGIS drone flight layer every
few minutes and tweets new flights: one tweet per flight when 1–3 show up, or a
single summary tweet when a bigger batch is uploaded.

```
NOPD drone flight: Tactical Operations
“Swat Roll”
Wed Sep 16, 2026, 4:46–4:57 PM (11 min)
Item #I-16149-26
https://www.google.com/maps/search/?api=1&query=29.95578,-90.01414
```

`src/dump.py` is the [original dump script](https://gist.github.com/mwollenweber/e99417f3a4ceec6dc9498939417dc15e),
unmodified; `src/app.py` imports its `fetch_pages`.

## How "new" works

NOPD uploads flights in batches, often days after they fly, and ObjectIds
aren't in takeoff order. So the Lambda doesn't look for "flights since the last
poll". Each run fetches every flight's attributes (about 150 KB), compares their
`flight_id`s with a DynamoDB table of flights it has already handled, and tweets
the new ones oldest first. Only a flight's bounding box is fetched for the map
link, because the full paths add up to more than 100 MB.

Guards against spamming:

- **First run** (empty table) records every existing flight and posts nothing.
- **Batches**: more than 3 new flights in one run go out as one tweet with the
  flight count, the dates they were flown, when NOPD logged them (the layer's
  last data edit) and how long after the flights that was, the purpose mix,
  and a link to the layer on an ArcGIS map:

  ```
  NOPD logged 63 drone flights at once
  Flown Oct 20–26, 2026
  Logged Thu Nov 5, 1:58 PM, 10–16 days later
  45 Call for Service, 6 Training, 4 Tactical Operations, 4 Special Event, 4 no purpose given
  Data: https://www.arcgis.com/apps/mapviewer/index.html?layers=918de1931f864bf3a0c10dd6b00499e9
  ```
- **Flood guard**: if more than `FloodThreshold` (default 200) new flights appear
  at once, which usually means the layer was rebuilt with new IDs, it posts
  nothing and fails the run. Invoke with `{"seed": true}` to accept them.
- **Per-run cap**: at most `MaxTweetsPerRun` (default 10) single-flight tweets.
  The rest go out on later runs. An X rate limit (429) also stops the run and
  leaves the rest for later.
- A flight is recorded only **after** its tweet succeeds, so failures are
  retried on the next run. If X rejects a tweet as a duplicate, the flight is
  recorded as already posted.

## Setup

1. **X app.** In the X developer portal, set the app's permissions to
   **Read and Write**, *then* generate the Access Token and Secret. A token
   generated before that change is read-only and gets a 403. You need the API
   Key/Secret and the Access Token/Secret for the account that will post.

2. **Deploy** (needs the AWS SAM CLI and Python 3.14):

   ```bash
   sam build && sam deploy --guided --stack-name nopd-drone-tweets
   ```

   Accept the defaults, or set `PollMinutes` and the other parameters. The
   first scheduled run seeds the table and needs no X credentials.

3. **Add the X credentials** to the secret the stack created:

   ```bash
   aws secretsmanager put-secret-value --secret-id nopd-drone-tweets/x-credentials \
     --secret-string '{"api_key":"…","api_secret":"…","access_token":"…","access_token_secret":"…"}'
   ```

4. **Check it.** A dry run logs what it would tweet without posting:

   ```bash
   aws lambda invoke --function-name <FunctionName output> \
     --payload '{"dry_run": true}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```

   To see a real tweet go out, delete one flight's row from the table. The
   next run treats it as new.

Logs go to the `/nopd-drone-tweets/poller` log group. A failed tweet or a
tripped flood guard ends the run with an error. If two runs fail within an hour,
the `PollerErrors` alarm emails `AlertEmail` (set it when deploying, e.g.
`sam deploy --parameter-overrides AlertEmail=you@example.com`, then confirm the
subscription email AWS sends).

## Usage

Everything below runs on your machine against the public layer and needs no
AWS or X credentials. Deploying the Lambda is covered under [Setup](#setup).

```bash
pip install -r requirements-dev.txt
python src/app.py --help
```

The script has two modes, one of which is required:

| Mode | What it does |
|---|---|
| `--preview N` | Print the tweet text for the N latest flights and exit. No state is kept. |
| `--watch` | Run the monitor locally: find flights not seen before, print each as JSON, remember them. |

### Preview the latest flights

```bash
python src/app.py --preview 3
```

```
NOPD drone flight: Training
Tue Oct 20, 2026, 3:21–3:29 PM (9 min)
https://www.google.com/maps/search/?api=1&query=29.95315,-90.06911

NOPD drone flight: Tactical Operations
“Swat Roll”
Wed Sep 16, 2026, 4:46–4:57 PM (11 min)
Item #I-16149-26
https://www.google.com/maps/search/?api=1&query=29.95578,-90.01414
...
```

### Run the monitor without X

`--watch` runs the same new-flight detection as the Lambda, but keeps the
"already handled" set in a local JSON file instead of DynamoDB and prints each
new flight to stdout as a pretty JSON object instead of tweeting it. Nothing is
sent anywhere.

```bash
python src/app.py --watch                 # one pass; the first pass only seeds the file
python src/app.py --watch --every 15      # keep polling every 15 minutes (Ctrl-C to stop)
python src/app.py --watch --state ~/flights.json --max-tweets 50
python src/app.py --watch 2>/dev/null     # flights only; progress logs go to stderr
python src/app.py --watch --every 1 2>/dev/null | jq -r .tweet   # just the tweet text
```

| Option | Default | Stands in for |
|---|---|---|
| `--state FILE` | `nopd-flights.json` | the DynamoDB table |
| `--every MINUTES` | one pass, then exit | the `PollMinutes` schedule |
| `--seed` | off | invoking with `{"seed": true}` |
| `--max-tweets N` | 10 | `MaxTweetsPerRun` |
| `--flood-threshold N` | 50 | `FloodThreshold` |

A typical first session:

```
$ python src/app.py --watch
08:35:33 INFO watching with state in nopd-flights.json
08:35:33 INFO seeded 456 flights without tweeting
08:35:33 INFO pass done: {"flights": 456, "seen": 0, "new": 456, "tweeted": 0, "failed": 0, "seeded": 456}

$ python src/app.py --watch --every 1       # later, after NOPD uploads a batch
08:50:34 INFO watching with state in nopd-flights.json
{
  "flight_id": "a61c65a5-e1ac-4040-88ed-5da0199a7dbe",
  "object_id": 427,
  "purpose": "Training",
  "description": null,
  "external_id": null,
  "takeoff": "2026-10-20T15:21:16-05:00",
  "landing": "2026-10-20T15:29:51-05:00",
  "duration_min": 9,
  "lat": 29.95315,
  "lon": -90.06911,
  "vehicle_serial": null,
  "dock_serial": null,
  "operation_id": null,
  "tweet": "NOPD drone flight: Training\nTue Oct 20, 2026, 3:21–3:29 PM (9 min)\nhttps://www.google.com/maps/search/?api=1&query=29.95315,-90.06911"
}
08:50:34 INFO tweeted a61c65a5-e1ac-4040-88ed-5da0199a7dbe as stdout
08:50:34 INFO summary {"flights": 457, "seen": 456, "new": 1, "tweeted": 1, "failed": 0}
```

Times are New Orleans local. `lat`/`lon` is the center of the flight path's
bounding box, and `tweet` is the exact text the Lambda would have posted.

The seeding, flood guard and per-pass cap behave exactly as deployed, so what
you see locally is what the Lambda would tweet. The state file holds the same
records as the table, keyed by `flight_id`; delete a flight's entry to see it
reported again on the next pass, or delete the file to start over (the next
pass seeds again and prints nothing).

### Tests

```bash
pytest
```

## Notes

- X API plans limit how many posts you can make. Keep
  `MaxTweetsPerRun` × runs per day under your plan's limit. Anything over it is
  rate-limited and waits for a later run.
- Times are shown in New Orleans local time. The map pin is the center of the
  flight path's bounding box.
- Cost is small: a few thousand Lambda invocations a month, DynamoDB on-demand,
  and $0.40/month for the secret.

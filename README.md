# Rain Informer 🌧️ (mumbai-rain-se-bacho-bot)

A serverless rain nowcasting bot. Runs on GitHub Actions every 5 minutes,
checks [Tomorrow.io](https://www.tomorrow.io/)'s minute-by-minute forecast for
one or more locations, and pushes a free, instant notification via
[ntfy.sh](https://ntfy.sh) when rain starts or is about to start. No SMS, no
DLT registration, no recurring cost.

You can also type **`Check`** into the ntfy app to get the last recorded
rainfall on demand (see [On-demand status](#on-demand-status-check)).

## How it works

| Piece | Role |
|---|---|
| `main.py` | Fetches the forecast, applies the alert logic, sends notifications |
| `state.json` | Per-location memory (last alert time, was-it-raining flag) — committed back to the repo by the Action |
| `.github/workflows/weather-check.yml` | Cron trigger (every 15 min) + state commit |
| ntfy.sh | Free push delivery to your phone's lock screen — no account needed |

## Alert logic

1. **Sudden rain** — dry → raining between checks: alert immediately, overrides cooldown.
2. **Expected rain** — dry now, rain (> threshold) predicted within the lookahead window, and the last alert was more than the cooldown ago: alert with an ETA.
3. **Persistent rain** — already raining last check and still raining: stay silent.
4. **Cooldown** — an "expected rain" alert was sent recently: stay silent until cooldown expires (rule 1 always overrides this).
5. **Clearing skies** — dry, nothing upcoming: reset state for the next wet cycle.

State is tracked **per location**, so each place runs its own independent cycle.

## On-demand status (`Check`)

Every poll stores the latest reading (rainfall in mm/hr + the time the API call
was made) into `state.json`. You can ask for it any time **without spending an
extra API call**:

1. In the ntfy app, open the subscription and use the **"Type a message…"**
   field at the bottom.
2. Send the word **`Check`** (also accepts `status` or `?`).
3. On the next poll the bot reads that message, and replies with the last
   stored reading, e.g.:

   > 📊 Rain status
   > Lake Bloom Residency: 0.12 mm/hr — dry ☀️
   > (checked 23 Jun, 12:58 IST)

**How it stays free:** reading messages from an ntfy topic is a plain HTTP
request to ntfy.sh — it costs **zero** Tomorrow.io quota. The reply uses the
*stored* reading, never a fresh forecast call.

**Latency:** there are two responders (whichever sees your `Check` first
answers):

- **Cloudflare Worker (≤60 s)** — see [cloudflare-worker/](cloudflare-worker/).
  A Durable Object wakes itself every ~60 s, polls the topic, and replies.
- **GitHub Actions (≤5 min)** — `main.py`'s built-in poll, the always-on
  fallback if the Worker is ever down.

> **ntfy free-tier limit:** ntfy.sh caps the number of *published* messages per
> day. Normal use (a few alerts + a few checks per day) stays well under it, but
> rapid repeated checks can temporarily exhaust the daily quota, after which
> replies return HTTP 429 until it resets (~24 h).

## Setup

### 1. Tomorrow.io API key
Sign up at [tomorrow.io](https://www.tomorrow.io/weather-api/) (free tier:
500 calls/day, 25/hour). At the 5-minute cadence this bot uses ~288 calls/day
for one location (12/hour) — within the free limits. Note: a second location
would double that to ~576/day and exceed the daily cap, so for two+ locations
either raise the cron interval or use a paid plan.

### 2. ntfy.sh topic
Install the **ntfy** app (Android/iOS), tap **+**, and subscribe to a unique,
hard-to-guess topic name. Anyone who knows the topic can read/post to it, so
make it unguessable (not just `rain`). Use the exact string you subscribed to
as the `NTFY_TOPIC` secret.

### 3. Configure your location(s)

Locations are **not secret**, so they live directly in the workflow file
(`.github/workflows/weather-check.yml`) in the `LOCATIONS` env var — a JSON
array. Each entry can specify the place **any of these ways**:

| Method | Entry shape | Example |
|---|---|---|
| **Exact coordinates (recommended)** | `{"name": "...", "lat": "...", "lon": "..."}` | `{"name":"Lake Bloom Residency","lat":"19.1173","lon":"72.9067"}` |
| **lat,lon as one string** | `{"name": "...", "location": "lat,lon"}` | `{"name":"Home","location":"19.1173,72.9067"}` |
| **Address / city name** | `{"name": "...", "location": "..."}` | `{"name":"Office","location":"Bandra Kurla Complex, Mumbai"}` |
| **Postal code** | `{"name": "...", "location": "..."}` | `{"name":"Home","location":"400076"}` |

Tomorrow.io geocodes string locations (addresses, city names, postal codes)
server-side, so no separate geocoding API is needed. For the most accurate
nowcasting (rain radar is hyperlocal), prefer exact `lat`/`lon` — a city-level
address can resolve several km from where you actually are.

To find exact coordinates: open Google Maps, long-press the spot, and the
`lat, lon` pair appears at the bottom.

**Currently configured:** Lake Bloom Residency (Powai / Andheri East),
`19.1173, 72.9067`. To add your second location, append another object to the
`LOCATIONS` array in the workflow, e.g.:

```yaml
LOCATIONS: >-
  [
    {"name": "Lake Bloom Residency", "lat": "19.1173", "lon": "72.9067"},
    {"name": "Office", "lat": "19.06xx", "lon": "72.86xx"}
  ]
```

> A single-location fallback also exists: if `LOCATIONS` is unset, the script
> reads `LAT`+`LON` or `LOCATION` env vars instead.

### 4. Add repository secrets

In GitHub: **Settings → Secrets and variables → Actions → New repository secret**.

Required secrets:
- `TOMORROW_API_KEY` — your Tomorrow.io key
- `NTFY_TOPIC` — the ntfy topic you subscribed to

(Locations are in the workflow file, not secrets.)

### 5. (Optional) tune thresholds

Set these as `env:` vars in the workflow if you want to override defaults:

| Variable | Default | Meaning |
|---|---|---|
| `RAIN_THRESHOLD_MM_HR` | `0.5` | mm/hr above which it counts as "raining" |
| `LOOKAHEAD_MINUTES` | `30` | how far ahead to scan for incoming rain |
| `COOLDOWN_HOURS` | `2` | minimum gap between "expected rain" alerts |

> During the Mumbai monsoon you may see steady light drizzle (~0.1 mm/hr).
> `0.5` filters that out so you only get pinged for real rain — lower it if
> you want to be warned about drizzle too.

### 6. Test it

**Actions** tab → **Rain Nowcasting Check** → **Run workflow** to trigger
manually and confirm a notification arrives (you can temporarily lower
`RAIN_THRESHOLD_MM_HR` to `0` to force a test alert).

## Instant `Check` responder (Cloudflare Worker)

[cloudflare-worker/](cloudflare-worker/) is an optional add-on that answers
`Check` in ~60 s instead of ~5 min. It's a Cloudflare Worker whose Durable
Object self-schedules via the Alarms API (~every 60 s), polls the ntfy topic,
and replies using the same public `state.json`. It is **stateless about
content** — it makes no Tomorrow.io calls.

Deploy:

```bash
cd cloudflare-worker
npm install
npx wrangler login                      # one-time, opens browser
npx wrangler secret put NTFY_TOPIC      # paste your topic
npx wrangler deploy
# Bootstrap the alarm loop once:
curl https://rain-informer-listener.<your-subdomain>.workers.dev
```

`GITHUB_STATE_URL` in `wrangler.toml` points at the raw `state.json` on GitHub —
update it if you fork/rename the repo.

> Cloudflare's **Cron Triggers did not fire** on the free account used here, so
> the Worker relies on **Durable Object alarms**, which do fire reliably. The
> cron entry in `wrangler.toml` is kept only as a harmless secondary nudge.

## Notes on the data field

Tomorrow.io's free `/v4/weather/forecast` endpoint returns `rainIntensity`
(plus separate sleet/snow/freezing-rain intensities), **not** a single
`precipitationIntensity` field. `main.py` handles both: it prefers
`precipitationIntensity` if present, otherwise sums the component intensities.

## Local testing

```bash
pip install -r requirements.txt
export TOMORROW_API_KEY=your_key
export NTFY_TOPIC=your_topic
export LOCATIONS='[{"name":"Lake Bloom Residency","lat":"19.1173","lon":"72.9067"}]'
python main.py
```

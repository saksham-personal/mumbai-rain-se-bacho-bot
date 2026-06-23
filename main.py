"""
Serverless rain nowcasting bot.

Runs on a schedule (GitHub Actions, every 15 min), checks Tomorrow.io's
minute-by-minute forecast for one or more configured locations, and pushes a
free notification via ntfy.sh when rain starts or is about to start.

Per-location state (last_alert_sent / was_raining_last_check) is persisted to
state.json so behaviour is consistent across cold, stateless runs.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("rain-informer")

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
RAIN_THRESHOLD_MM_HR = float(os.environ.get("RAIN_THRESHOLD_MM_HR", "0.5"))
LOOKAHEAD_MINUTES = int(os.environ.get("LOOKAHEAD_MINUTES", "30"))
COOLDOWN_HOURS = float(os.environ.get("COOLDOWN_HOURS", "2"))
REQUEST_TIMEOUT_SECONDS = 15
MAX_API_RETRIES = 3

DEFAULT_LOCATION_STATE = {"last_alert_sent": None, "was_raining_last_check": False}


class ConfigError(Exception):
    """Raised for missing/invalid configuration. Fatal — fix and re-run."""


class WeatherAPIError(Exception):
    """Raised when the forecast can't be fetched/parsed after retries."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _location_string_from_entry(entry: dict) -> str:
    """
    Turn one location config entry into a Tomorrow.io 'location' value.

    An entry may specify the place any of these ways:
      - {"lat": "19.1173", "lon": "72.9067"}   -> exact coordinates (best)
      - {"location": "19.1173,72.9067"}        -> lat,lon as one string
      - {"location": "Powai, Mumbai, India"}   -> address / city name
      - {"location": "400076"}                 -> postal code
    Tomorrow.io geocodes string locations server-side, so addresses, city
    names and postal codes work without a separate geocoding call.
    """
    lat = str(entry.get("lat", "")).strip()
    lon = str(entry.get("lon", "")).strip()
    location = str(entry.get("location", "")).strip()

    if lat and lon:
        try:
            float(lat)
            float(lon)
        except ValueError as exc:
            raise ConfigError(f"lat/lon must be numeric, got lat={lat!r} lon={lon!r}") from exc
        return f"{lat},{lon}"

    if location:
        return location

    raise ConfigError(f"Location entry has neither lat/lon nor 'location': {entry!r}")


def resolve_locations() -> list:
    """
    Resolve the list of locations to monitor.

    Precedence:
      1. LOCATIONS env var — a JSON array of entries, e.g.
         [{"name":"Lake Bloom","lat":"19.1173","lon":"72.9067"}, ...]
      2. Single-location fallback via LAT+LON or LOCATION env vars.

    Returns a list of {"name": str, "query": str} dicts.
    """
    raw = os.environ.get("LOCATIONS", "").strip()
    if raw:
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"LOCATIONS is not valid JSON: {exc}") from exc
        if not isinstance(entries, list) or not entries:
            raise ConfigError("LOCATIONS must be a non-empty JSON array.")

        locations = []
        seen_names = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ConfigError(f"Each LOCATIONS entry must be an object, got: {entry!r}")
            query = _location_string_from_entry(entry)
            name = str(entry.get("name", "")).strip() or query
            if name in seen_names:
                raise ConfigError(f"Duplicate location name {name!r} — names must be unique.")
            seen_names.add(name)
            locations.append({"name": name, "query": query})
        return locations

    # Single-location fallback.
    lat = os.environ.get("LAT", "").strip()
    lon = os.environ.get("LON", "").strip()
    location = os.environ.get("LOCATION", "").strip()
    if lat and lon:
        query = _location_string_from_entry({"lat": lat, "lon": lon})
    elif location:
        query = location
    else:
        raise ConfigError(
            "No location configured. Set LOCATIONS (a JSON array), or "
            "LAT + LON, or LOCATION as environment variables / repo secrets."
        )
    return [{"name": query, "query": query}]


def load_config() -> dict:
    api_key = os.environ.get("TOMORROW_API_KEY", "").strip()
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()

    missing = []
    if not api_key:
        missing.append("TOMORROW_API_KEY")
    if not ntfy_topic:
        missing.append("NTFY_TOPIC")
    if missing:
        raise ConfigError(f"Missing required environment variable(s): {', '.join(missing)}")

    return {
        "api_key": api_key,
        "ntfy_topic": ntfy_topic,
        "locations": resolve_locations(),
    }


# --------------------------------------------------------------------------
# State persistence (per-location)
# --------------------------------------------------------------------------

def load_state() -> dict:
    """
    Returns a dict keyed by location name:
        {"<name>": {"last_alert_sent": ..., "was_raining_last_check": bool}}

    Transparently migrates the old flat single-location format.
    """
    if not os.path.exists(STATE_FILE):
        log.info("No existing state file, starting fresh.")
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("state.json unreadable/corrupt (%s) — resetting to defaults.", exc)
        return {}

    if not isinstance(data, dict):
        return {}

    # Old flat format: {"last_alert_sent": ..., "was_raining_last_check": ...}
    if "was_raining_last_check" in data or "last_alert_sent" in data:
        log.info("Migrating legacy flat state.json to per-location format.")
        return {}

    # New format is preserved as-is: per-location entries plus a "_meta" block.
    # Unknown keys (e.g. last_observation) are kept so the status command works.
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def get_location_state(state: dict, name: str) -> dict:
    """Return this location's state, merged over defaults so missing keys are safe."""
    result = dict(DEFAULT_LOCATION_STATE)
    entry = state.get(name)
    if isinstance(entry, dict):
        result["last_alert_sent"] = entry.get("last_alert_sent")
        result["was_raining_last_check"] = bool(entry.get("was_raining_last_check", False))
        if "last_observation" in entry:
            result["last_observation"] = entry["last_observation"]
    return result


def save_state(state: dict) -> None:
    """Atomic write so a crash mid-write never corrupts state.json."""
    tmp_path = f"{STATE_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, STATE_FILE)
    log.info("State saved for %d location(s).", len(state))


def parse_state_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        log.warning("Could not parse stored timestamp %r — treating as unset.", value)
        return None


# --------------------------------------------------------------------------
# Weather API
# --------------------------------------------------------------------------

def fetch_forecast(api_key: str, query: str):
    """
    Fetch minute-by-minute precipitation intensity for the next ~hour.

    Returns a list of (datetime_utc, precipitation_intensity_mm_hr) tuples,
    sorted chronologically, the first entry being the current minute.
    """
    url = "https://api.tomorrow.io/v4/weather/forecast"
    params = {
        "location": query,
        "apikey": api_key,
        "timesteps": "1m",
        "units": "metric",
    }

    last_exc = None
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            # Back off and retry on rate-limit (429) and transient 5xx.
            if resp.status_code == 429 or resp.status_code >= 500:
                resp.raise_for_status()
            resp.raise_for_status()
            data = resp.json()
            return _parse_minutely_intervals(data)
        except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
            last_exc = exc
            log.warning("Forecast fetch attempt %d/%d failed: %s", attempt, MAX_API_RETRIES, exc)
            if attempt < MAX_API_RETRIES:
                time.sleep(2 ** attempt)  # 2s, 4s backoff

    raise WeatherAPIError(f"Failed to fetch forecast after {MAX_API_RETRIES} attempts: {last_exc}")


def _precip_intensity(values: dict) -> float:
    """
    Extract precipitation intensity (mm/hr) from one interval's values.

    Tomorrow.io's free /weather/forecast endpoint does NOT return a single
    `precipitationIntensity` field — it returns separate rain/sleet/snow/
    freezing-rain intensities. We prefer `precipitationIntensity` if present
    (some plans/endpoints expose it), otherwise sum the component intensities.
    """
    if values.get("precipitationIntensity") is not None:
        return float(values["precipitationIntensity"])

    components = (
        "rainIntensity",
        "sleetIntensity",
        "snowIntensity",
        "freezingRainIntensity",
    )
    total = 0.0
    found = False
    for key in components:
        val = values.get(key)
        if val is not None:
            total += float(val)
            found = True
    if not found:
        raise KeyError("no precipitation intensity field in interval values")
    return total


def _parse_minutely_intervals(data: dict):
    try:
        timelines = data["timelines"]["minutely"]
    except (KeyError, TypeError) as exc:
        raise WeatherAPIError(f"Unexpected API response shape: {data!r}") from exc

    if not timelines:
        raise WeatherAPIError("API returned an empty minutely forecast.")

    intervals = []
    for entry in timelines:
        try:
            ts_raw = entry["time"]
            intensity = _precip_intensity(entry["values"])
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed forecast interval %r (%s)", entry, exc)
            continue
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        intervals.append((ts, intensity))

    if not intervals:
        raise WeatherAPIError("No usable forecast intervals after parsing.")

    intervals.sort(key=lambda pair: pair[0])
    return intervals


# --------------------------------------------------------------------------
# Notification delivery
# --------------------------------------------------------------------------

def send_push_notification(topic: str, message: str, title: str = "Rain Informer") -> bool:
    url = f"https://ntfy.sh/{topic}"
    try:
        resp = requests.post(
            url,
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Tags": "rain,umbrella"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        log.info("Notification sent: %s", message)
        return True
    except requests.exceptions.RequestException as exc:
        log.error("Failed to send ntfy.sh notification: %s", exc)
        return False


# --------------------------------------------------------------------------
# Interactive "Check" command (read from the ntfy topic, reply with last state)
# --------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))
COMMAND_WORDS = {"check", "status", "?"}
# How far back to look for commands on the very first run (no stored marker).
COMMAND_FIRST_RUN_WINDOW_SECONDS = 600


def read_ntfy_commands(topic: str, since_unix: int) -> list:
    """
    Read messages published to the ntfy topic since `since_unix` and return
    those that look like a status request (e.g. you typed "Check" in the app).

    Reading ntfy is free and uses NO Tomorrow.io quota. Best-effort: on any
    network/parse error we just return [] and try again next run.
    """
    url = f"https://ntfy.sh/{topic}/json"
    params = {"poll": "1", "since": int(since_unix)}
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.warning("Could not read ntfy messages (command check skipped): %s", exc)
        return []

    commands = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("event") != "message":
            continue
        body = str(msg.get("message", "")).strip().lower()
        if body in COMMAND_WORDS:
            commands.append(msg)
    return commands


def build_status_message(config: dict, state: dict) -> str:
    """Compose a human-readable summary of each location's last stored reading."""
    lines = []
    for location in config["locations"]:
        name = location["name"]
        obs = get_location_state(state, name).get("last_observation")
        if not obs:
            lines.append(f"{name}: no reading recorded yet")
            continue
        mm = obs.get("intensity_mm_hr")
        raining = obs.get("is_raining")
        status = "raining 🌧️" if raining else "dry ☀️"
        try:
            when = datetime.fromisoformat(obs["observed_at"]).astimezone(IST).strftime("%d %b, %H:%M")
        except (KeyError, ValueError, TypeError):
            when = "unknown time"
        lines.append(f"{name}: {mm} mm/hr — {status}\n(checked {when} IST)")
    return "📊 Rain status\n" + "\n".join(lines)


def handle_commands(config: dict, state: dict) -> None:
    """
    Poll the ntfy topic for "Check"-style messages and reply with the last
    stored readings. Updates state["_meta"]["last_command_ts"] so the same
    message is never answered twice.
    """
    topic = config["ntfy_topic"]
    poll_start = int(time.time())

    meta = state.get("_meta", {})
    since = meta.get("last_command_ts", poll_start - COMMAND_FIRST_RUN_WINDOW_SECONDS)

    commands = read_ntfy_commands(topic, since)

    # Advance the marker regardless, so we never re-answer older messages.
    state["_meta"] = {**meta, "last_command_ts": poll_start}

    if not commands:
        return

    log.info("Received %d status command(s); replying with last readings.", len(commands))
    send_push_notification(topic, build_status_message(config, state), title="Rain Status")


# --------------------------------------------------------------------------
# Core decision logic (per location)
# --------------------------------------------------------------------------

def check_location(config: dict, location: dict, state: dict) -> bool:
    """
    Run the alert logic for a single location, mutating `state[name]` in place.

    Returns True on success, False if this location needs a retry next run
    (forecast fetch failed, or a notification failed to send).
    """
    name = location["name"]
    query = location["query"]
    loc_state = get_location_state(state, name)
    last_alert_sent = parse_state_timestamp(loc_state["last_alert_sent"])
    was_raining_last_check = loc_state["was_raining_last_check"]

    now = datetime.now(timezone.utc)

    try:
        forecast = fetch_forecast(config["api_key"], query)
    except WeatherAPIError as exc:
        log.error("[%s] Skipping — keeping prior state for retry: %s", name, exc)
        return False  # leave this location's state untouched

    current_time, current_intensity = forecast[0]
    current_is_raining = current_intensity > RAIN_THRESHOLD_MM_HR
    log.info(
        "[%s] Current intensity: %.2f mm/hr (raining=%s) at %s",
        name, current_intensity, current_is_raining, current_time.isoformat(),
    )

    topic = config["ntfy_topic"]

    # Always remember the latest reading so the "Check" command can report it
    # without spending another API call.
    observation = {
        "intensity_mm_hr": round(current_intensity, 3),
        "is_raining": current_is_raining,
        "observed_at": now.isoformat(),
    }

    def commit(last_alert_sent_value, raining_value):
        state[name] = {
            "last_alert_sent": last_alert_sent_value,
            "was_raining_last_check": raining_value,
            "last_observation": observation,
        }

    if current_is_raining:
        if not was_raining_last_check:
            # Edge case 1: sudden dry -> wet transition. Alert, override cooldown.
            sent = send_push_notification(
                topic,
                f"🚨 Sudden Rain at {name}: It has just started raining! "
                f"({current_intensity:.2f} mm/hr)",
            )
            if sent:
                commit(now.isoformat(), True)
                return True
            log.warning("[%s] Sudden-rain alert failed — will retry next run.", name)
            commit(loc_state["last_alert_sent"], False)  # record reading, keep alert state for retry
            return False

        # Edge case 3: already raining last check — stay silent.
        commit(loc_state["last_alert_sent"], True)
        return True

    # Currently dry — scan the next LOOKAHEAD_MINUTES for incoming rain.
    upcoming = None
    for ts, intensity in forecast[1:]:
        minutes_out = (ts - now).total_seconds() / 60
        if minutes_out > LOOKAHEAD_MINUTES:
            break
        if intensity > RAIN_THRESHOLD_MM_HR:
            upcoming = (ts, minutes_out, intensity)
            break

    if upcoming is not None:
        _, minutes_out, upcoming_intensity = upcoming
        minutes_out = max(1, round(minutes_out))
        cooldown_elapsed = (
            last_alert_sent is None
            or (now - last_alert_sent) > timedelta(hours=COOLDOWN_HOURS)
        )

        if cooldown_elapsed:
            # Edge case 2: expected rain, outside cooldown window.
            sent = send_push_notification(
                topic,
                f"⚠️ Rain Warning ({name}): Precipitation expected to start in {minutes_out} minutes "
                f"(~{upcoming_intensity:.2f} mm/hr).",
            )
            if sent:
                commit(now.isoformat(), False)
                return True
            log.warning("[%s] Expected-rain alert failed — keeping prior cooldown for retry.", name)
            commit(loc_state["last_alert_sent"], False)
            return False

        # Edge case 4: cooldown still active — stay silent.
        log.info("[%s] Upcoming rain but cooldown active (%sh). Silent.", name, COOLDOWN_HOURS)
        commit(loc_state["last_alert_sent"], False)
        return True

    # Edge case 5: clear skies — reset for the next wet cycle.
    commit(loc_state["last_alert_sent"], False)
    return True


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    state = load_state()
    all_ok = True
    for location in config["locations"]:
        log.info("Checking weather for %r (%s)", location["name"], location["query"])
        try:
            ok = check_location(config, location, state)
            all_ok = all_ok and ok
        except Exception as exc:  # never let one location kill the whole run
            log.exception("[%s] Unexpected error: %s", location["name"], exc)
            all_ok = False

    # Answer any "Check" messages typed into the ntfy app (free, no API quota).
    try:
        handle_commands(config, state)
    except Exception as exc:
        log.exception("Command handling failed: %s", exc)

    save_state(state)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

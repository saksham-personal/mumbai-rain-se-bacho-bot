// Rain Informer — fast "Check" responder (Cloudflare Worker + Durable Object).
//
// The Durable Object wakes itself ~every 60s via the Alarms API, polls the ntfy
// topic, and replies to "Check"/"status"/"?" with the last reading already
// stored in state.json by the GitHub Actions bot (main.py). It makes NO
// Tomorrow.io API calls — it only reads the ntfy topic + the public state.json.
//
// Why alarms (not cron): Cloudflare Cron Triggers did not fire on this free
// account, so the DO self-schedules via storage alarms, which DO fire reliably
// and survive eviction.
//
// Dedup: a single stored timestamp ("handledCommandTs") of the newest command
// we've already acted on. We reply only when a newer command appears. This does
// NOT depend on our own reply showing up in the topic, so a failed send (e.g.
// ntfy's daily message limit) never causes a retry storm.
//
// Note on limits: ntfy.sh's free tier caps daily *published* messages per
// visitor. Normal use (a handful of alerts + checks/day) is well within it.

const COMMAND_WORDS = new Set(["check", "status", "?"]);
const REPLY_TITLE = "Rain Status";
const LOOKBACK = "600s";       // window of topic history scanned each poll
const HEARTBEAT_MS = 60000;    // alarm cadence -> worst-case latency ~60s

export class NtfyListener {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request) {
    await this.ensureAlarm();
    const diag = await this.poll("fetch");
    return new Response(JSON.stringify(diag), {
      headers: { "Content-Type": "application/json" },
    });
  }

  async ensureAlarm() {
    if ((await this.state.storage.getAlarm()) === null) {
      await this.state.storage.setAlarm(Date.now() + 1000);
    }
  }

  async alarm() {
    try {
      await this.poll("alarm");
    } finally {
      await this.state.storage.setAlarm(Date.now() + HEARTBEAT_MS);
    }
  }

  async poll(caller) {
    const diag = { ok: false, caller: caller || "?", at: Math.floor(Date.now() / 1000) };
    const topic = this.env.NTFY_TOPIC;
    if (!topic) {
      diag.error = "NTFY_TOPIC secret not set";
      return diag;
    }

    let text;
    try {
      const resp = await fetch(`https://ntfy.sh/${topic}/json?poll=1&since=${LOOKBACK}`, {
        headers: { "Cache-Control": "no-cache" },
      });
      diag.ntfyStatus = resp.status;
      if (!resp.ok) {
        diag.error = "ntfy poll status " + resp.status;
        return diag;
      }
      text = await resp.text();
    } catch (err) {
      diag.error = "ntfy poll error: " + String(err);
      return diag;
    }

    // Newest command timestamp seen in the lookback window.
    let lastCommand = 0;
    for (const line of text.split("\n")) {
      if (!line.trim()) continue;
      let m;
      try { m = JSON.parse(line); } catch { continue; }
      if (m.event !== "message" || m.title === REPLY_TITLE) continue;
      const ts = typeof m.time === "number" ? m.time : 0;
      const body = String(m.message || "").trim().toLowerCase();
      if (COMMAND_WORDS.has(body) && ts > lastCommand) lastCommand = ts;
    }

    const handledTs = (await this.state.storage.get("handledCommandTs")) || 0;
    diag.lastCommand = lastCommand;
    diag.handledTs = handledTs;

    if (lastCommand > handledTs) {
      diag.publishResult = await this.publish(await this.buildStatusMessage());
      await this.state.storage.put("handledCommandTs", lastCommand);
      diag.replied = true;
    }

    diag.ok = true;
    return diag;
  }

  async buildStatusMessage() {
    try {
      const resp = await fetch(this.env.GITHUB_STATE_URL, {
        headers: { "Cache-Control": "no-cache" },
      });
      if (!resp.ok) throw new Error(`state.json fetch failed: ${resp.status}`);
      const stateData = await resp.json();

      const lines = [];
      for (const [name, entry] of Object.entries(stateData)) {
        if (name === "_meta" || typeof entry !== "object" || entry === null) continue;
        const obs = entry.last_observation;
        if (!obs) {
          lines.push(`${name}: no reading recorded yet`);
          continue;
        }
        const status = obs.is_raining ? "raining \u{1F327}\u{FE0F}" : "dry ☀\u{FE0F}";
        let when = "unknown time";
        try {
          when = new Date(obs.observed_at).toLocaleString("en-IN", {
            timeZone: "Asia/Kolkata",
            day: "2-digit", month: "short",
            hour: "2-digit", minute: "2-digit", hour12: false,
          });
        } catch { /* keep default */ }
        lines.push(`${name}: ${obs.intensity_mm_hr} mm/hr — ${status}\n(last checked ${when} IST)`);
      }

      if (lines.length === 0) return "No location data found yet.";
      return "\u{1F4CA} Rain status\n" + lines.join("\n");
    } catch (err) {
      return "⚠️ Could not fetch the latest rain status right now — try again shortly.";
    }
  }

  async publish(message) {
    try {
      const resp = await fetch(`https://ntfy.sh/${this.env.NTFY_TOPIC}`, {
        method: "POST",
        headers: { Title: REPLY_TITLE },
        body: message,
      });
      return { status: resp.status };
    } catch (err) {
      return { error: String(err) };
    }
  }
}

function listenerStub(env) {
  const id = env.NTFY_LISTENER.idFromName("singleton");
  return env.NTFY_LISTENER.get(id);
}

export default {
  async fetch(request, env) {
    return await listenerStub(env).fetch("https://internal/poll");
  },
  async scheduled(event, env, ctx) {
    // If cron ever fires, it just nudges the same poll; alarms are the primary.
    ctx.waitUntil(listenerStub(env).fetch("https://internal/poll"));
  },
};

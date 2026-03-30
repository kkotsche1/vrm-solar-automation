import { startTransition, useCallback, useEffect, useRef, useState } from "react";

const POLL_INTERVAL_MS = 60000;
const COUNTDOWN_SECONDS = 60;

const WEATHER_CODE_LABELS = {
  0: "Clear sky",
  1: "Mainly clear",
  2: "Partly cloudy",
  3: "Overcast",
  45: "Fog",
  48: "Depositing rime fog",
  51: "Light drizzle",
  53: "Moderate drizzle",
  55: "Dense drizzle",
  56: "Light freezing drizzle",
  57: "Dense freezing drizzle",
  61: "Slight rain",
  63: "Moderate rain",
  65: "Heavy rain",
  66: "Light freezing rain",
  67: "Heavy freezing rain",
  71: "Slight snow",
  73: "Moderate snow",
  75: "Heavy snow",
  77: "Snow grains",
  80: "Slight rain showers",
  81: "Moderate rain showers",
  82: "Violent rain showers",
  85: "Slight snow showers",
  86: "Heavy snow showers",
  95: "Thunderstorm",
  96: "Thunderstorm with slight hail",
  99: "Thunderstorm with heavy hail",
};

const WEATHER_MODE_LABELS = {
  cooling: "Cooling demand",
  heating: "Heating demand",
  mild: "Mild day",
  mixed: "Mixed conditions",
  unknown: "Unknown conditions",
};

const OVERRIDE_LABELS = {
  manual_on_until: "Manual ON timer",
  manual_off_until: "Manual OFF timer",
  manual_off_until_next_auto_on: "Hold OFF until auto ON",
  emergency_off: "Emergency OFF",
};

function App() {
  const [status, setStatus] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");
  const [pendingAction, setPendingAction] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [overrideMinutes, setOverrideMinutes] = useState("60");
  const [lastUpdatedAt, setLastUpdatedAt] = useState("");
  const [countdown, setCountdown] = useState(COUNTDOWN_SECONDS);
  const countdownRef = useRef(COUNTDOWN_SECONDS);
  const rafIdRef = useRef(0);
  const lastTickRef = useRef(Date.now());

  const resetCountdown = useCallback(() => {
    countdownRef.current = COUNTDOWN_SECONDS;
    lastTickRef.current = Date.now();
    setCountdown(COUNTDOWN_SECONDS);
  }, []);

  const applyStatusPayload = useCallback((payload) => {
    startTransition(() => {
      setStatus(payload);
      setError("");
      setLastUpdatedAt(new Date().toISOString());
    });
    resetCountdown();
  }, [resetCountdown]);

  const fetchStatus = useCallback(async ({ silent = false } = {}) => {
    if (!silent) {
      setIsLoading(true);
    }

    try {
      const response = await fetch("/api/status");
      const payload = await readJson(response);
      if (!response.ok) {
        throw new Error(extractError(payload, "Unable to load system status."));
      }

      applyStatusPayload(payload);
    } catch (fetchError) {
      startTransition(() => {
        setError(fetchError.message || "Unable to load system status.");
      });
    } finally {
      if (!silent) {
        setIsLoading(false);
      }
    }
  }, [applyStatusPayload]);

  // Smooth countdown animation via requestAnimationFrame
  useEffect(() => {
    function tick() {
      const now = Date.now();
      const elapsed = (now - lastTickRef.current) / 1000;
      lastTickRef.current = now;
      countdownRef.current = Math.max(0, countdownRef.current - elapsed);
      setCountdown(countdownRef.current);
      rafIdRef.current = requestAnimationFrame(tick);
    }
    rafIdRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafIdRef.current);
  }, []);

  // SSE connection with polling fallback
  useEffect(() => {
    void fetchStatus();

    let eventSource;
    let fallbackIntervalId;
    let retryTimeoutId;
    let isDisposed = false;

    function clearFallbackPolling() {
      if (fallbackIntervalId) {
        window.clearInterval(fallbackIntervalId);
        fallbackIntervalId = null;
      }
    }

    function scheduleReconnect() {
      if (isDisposed || retryTimeoutId) {
        return;
      }
      retryTimeoutId = window.setTimeout(() => {
        retryTimeoutId = null;
        connectSSE();
      }, 5000);
    }

    function connectSSE() {
      if (isDisposed) {
        return;
      }

      eventSource = new EventSource("/api/events");

      eventSource.addEventListener("status_update", (event) => {
        try {
          const payload = JSON.parse(event.data);
          applyStatusPayload(payload);
        } catch {
          // Ignore malformed events
        }
      });

      eventSource.onerror = () => {
        eventSource.close();
        // Fall back to polling and retry SSE later
        if (!fallbackIntervalId) {
          fallbackIntervalId = window.setInterval(() => {
            void fetchStatus({ silent: true });
          }, POLL_INTERVAL_MS);
        }
        scheduleReconnect();
      };

      // If SSE connects, stop fallback polling
      eventSource.onopen = () => {
        clearFallbackPolling();
      };
    }

    connectSSE();

    return () => {
      isDisposed = true;
      if (eventSource) {
        eventSource.close();
      }
      clearFallbackPolling();
      if (retryTimeoutId) {
        window.clearTimeout(retryTimeoutId);
      }
    };
  }, [fetchStatus, applyStatusPayload]);

  async function runAction(label, url, options = {}) {
    setPendingAction(label);
    setActionMessage("");

    try {
      const response = await fetch(url, options);
      const payload = await readJson(response);
      if (!response.ok) {
        throw new Error(extractError(payload, `${label} failed.`));
      }

      setActionMessage(describeActionResult(label, payload));
      setError("");
      await fetchStatus({ silent: true });
    } catch (actionError) {
      const message = actionError.message || `${label} failed.`;
      setActionMessage(message);
      setError(message);
    } finally {
      setPendingAction("");
    }
  }

  function submitTimedOverride(mode) {
    const minutes = Number.parseFloat(overrideMinutes);
    if (!Number.isFinite(minutes) || minutes <= 0) {
      setActionMessage("Override duration must be greater than zero minutes.");
      return;
    }

    const path = mode === "on" ? "/api/override/on" : "/api/override/off";
    const label = mode === "on" ? "Manual ON override" : "Manual OFF override";
    void runAction(label, path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ minutes }),
    });
  }

  const power = status?.power ?? {};
  const weather = status?.weather ?? {};
  const powerStatus = status?.power_status ?? {};
  const decision = status?.decision ?? {};
  const override = status?.override ?? {};
  const controlLoop = status?.control_loop ?? {};
  const nextState = status?.next_state ?? {};
  const previousState = status?.previous_state ?? null;
  const plug = status?.plug ?? {};
  const plugStatus = plug.status ?? {};

  const automaticTargetIsOn = Boolean(decision.should_turn_on);
  const effectiveTargetIsOn = Boolean(override.effective_target_is_on);
  const emergencyOffActive = override.mode === "emergency_off";
  const plugOutputIsOn = plugStatus.output ?? nextState.last_known_plug_is_on ?? null;
  const weatherModeLabel = WEATHER_MODE_LABELS[decision.weather_mode] ?? "Unknown conditions";
  const weatherCodeLabel = WEATHER_CODE_LABELS[weather.weather_code] ?? "Unavailable";
  const activeOverrideLabel = override.mode ? (OVERRIDE_LABELS[override.mode] ?? override.mode) : "Automatic";
  const powerUnavailableMessage = powerStatus.available === false ? (powerStatus.error || "Power data is currently unavailable.") : "";

  return (
    <main className="shell">
      <div className="background-orb orb-a" />
      <div className="background-orb orb-b" />
      <div className="background-orb orb-c" />

      <button
        className={`emergency-button ${emergencyOffActive ? "active" : ""}`}
        type="button"
        onClick={() => {
          void runAction(
            emergencyOffActive ? "Resume automatic control" : "Emergency OFF",
            emergencyOffActive ? "/api/override" : "/api/override/emergency-off",
            { method: emergencyOffActive ? "DELETE" : "POST" },
          );
        }}
        disabled={pendingAction !== ""}
      >
        {pendingAction === "Emergency OFF"
          ? "Applying..."
          : pendingAction === "Resume automatic control"
            ? "Restoring..."
            : emergencyOffActive
              ? "Return to automatic"
              : "Emergency Off"}
      </button>



      <section className="toolbar glass-panel">
        <div className="live-stats-container">
          <div className="live-stats-row">
            <div className={`live-stat-chip tone-${plugOutputIsOn ? "info" : "muted"}`}>
              <span className="stat-label">Plug</span>
              <span className="stat-value">{describeBinary(plugOutputIsOn, "ON", "OFF")}</span>
            </div>
            <div className={`live-stat-chip tone-${Number(power.battery_soc_percent) >= 60 ? "good" : Number(power.battery_soc_percent) > 50 ? "warn" : "danger"}`}>
              <span className="stat-label">Battery</span>
              <span className="stat-value">{formatPercent(power.battery_soc_percent)}</span>
            </div>
            <div className={`live-stat-chip tone-${Number(power.solar_watts) > 0 ? "info" : "muted"}`}>
              <span className="stat-label">Solar</span>
              <span className="stat-value">{formatWatts(power.solar_watts)}</span>
            </div>
            <div className={`live-stat-chip tone-${Number(power.house_watts) > 4000 ? "warn" : "muted"}`}>
              <span className="stat-label">House</span>
              <span className="stat-value">{formatWatts(power.house_watts)}</span>
            </div>
            <div className={`live-stat-chip tone-${Math.abs(Number(power.generator_watts)) >= 100 ? "danger" : "muted"}`}>
              <span className="stat-label">Generator</span>
              <span className="stat-value">{formatWatts(power.generator_watts)}</span>
            </div>
            <div className={`live-stat-chip tone-${override.is_active ? "warn" : "info"}`}>
              <span className="stat-label">Mode</span>
              <span className="stat-value">{override.is_active ? activeOverrideLabel : "Auto"}</span>
            </div>
          </div>
          <p className="toolbar-meta">
            {isLoading && !status ? "Loading live system state..." : `Updated ${formatRelative(lastUpdatedAt)}`}
          </p>
        </div>

        <div className="toolbar-actions">
          <button
            className="refresh-button secondary-button"
            type="button"
            onClick={() => {
              resetCountdown();
              void fetchStatus();
            }}
            disabled={isLoading || pendingAction !== ""}
          >
            <CountdownRing seconds={countdown} total={COUNTDOWN_SECONDS} />
            <span>Refresh</span>
          </button>
        </div>
      </section>

      {error ? (
        <section className="alert-panel glass-panel">
          <p className="alert-title">Status issue</p>
          <p>{error}</p>
        </section>
      ) : null}

      {powerUnavailableMessage ? (
        <section className="alert-panel glass-panel">
          <p className="alert-title">Power telemetry unavailable</p>
          <p>{powerUnavailableMessage}</p>
        </section>
      ) : null}

      {actionMessage ? (
        <section className="alert-panel glass-panel success">
          <p className="alert-title">Controller response</p>
          <p>{actionMessage}</p>
        </section>
      ) : null}

      {controlLoop.last_error ? (
        <section className="alert-panel glass-panel">
          <p className="alert-title">Automatic control loop issue</p>
          <p>{controlLoop.last_error}</p>
        </section>
      ) : null}

      <section className="dashboard-grid">
        <Panel
          title="Energy overview"
          kicker={power.site_name || "Local system"}
          aside={
            powerStatus.available === false
              ? "Off-network fallback"
              : power.queried_at_iso
                ? `Power sampled ${formatDateTime(power.queried_at_iso)}`
                : "Awaiting power sample"
          }
        >
          <div className="metric-grid">
            <DataTile label="Battery SOC" value={formatPercent(power.battery_soc_percent)} />
            <DataTile label="Solar production" value={formatWatts(power.solar_watts)} />
            <DataTile label="House load" value={formatWatts(power.house_watts)} />
            <DataTile label="Generator power" value={formatWatts(power.generator_watts)} />
          </div>

          <div className="meter-stack">
            <ProgressMeter
              label="Battery reserve"
              value={coercePercent(power.battery_soc_percent)}
              tone={coercePercent(power.battery_soc_percent) >= 60 ? "good" : coercePercent(power.battery_soc_percent) > 50 ? "warn" : "danger"}
            />
            <ProgressMeter
              label="Solar output"
              value={capPercent(power.solar_watts, 4000)}
              tone={Number(power.solar_watts) > 0 ? "info" : "muted"}
            />
            <ProgressMeter
              label="Generator presence"
              value={capPercent(absValue(power.generator_watts), 1500)}
              tone={absValue(power.generator_watts) >= 100 ? "danger" : "muted"}
            />
          </div>

          <div className="detail-row">
            <DetailPill label="Site identifier" value={power.site_identifier || "Unavailable"} />
            <DetailPill label="Input source" value={formatInputSource(power.active_input_source)} />
            <DetailPill label="Telemetry" value={powerStatus.available === false ? "Unavailable" : "Live"} />
            <DetailPill label="House L1" value={formatWatts(power.house_l1_watts)} />
            <DetailPill label="House L2" value={formatWatts(power.house_l2_watts)} />
            <DetailPill label="House L3" value={formatWatts(power.house_l3_watts)} />
          </div>
        </Panel>

        <Panel
          title="Controller state"
          kicker={humanizeAction(decision.action)}
          aside={override.is_active ? activeOverrideLabel : "Automatic mode"}
        >
          <div className="controller-grid">
            <StateBlock
              label="Automatic target"
              value={automaticTargetIsOn ? "Pump ON" : "Pump OFF"}
              detail={decision.reason || "Waiting for fresh evaluation"}
            />
            <StateBlock
              label="Effective target"
              value={effectiveTargetIsOn ? "Pump ON" : "Pump OFF"}
              detail={override.reason || "No override currently modifying the target"}
            />
            <StateBlock
              label="Remembered state"
              value={nextState.is_on ? "Controller remembers ON" : "Controller remembers OFF"}
              detail={nextState.changed_at_iso ? `Changed ${formatDateTime(nextState.changed_at_iso)}` : "No remembered change time yet"}
            />
            <StateBlock
              label="Shelly reachability"
              value={plug.reachable ? "Reachable" : "Unavailable"}
              detail={plug.reachable ? describeBinary(plugStatus.output, "Output ON", "Output OFF") : plug.error || "No Shelly data"}
            />
          </div>

          <div className="reason-list">
            <p className="section-caption">Decision reasons</p>
            {Array.isArray(decision.reasons) && decision.reasons.length > 0 ? (
              decision.reasons.map((reason) => (
                <div className="reason-item" key={reason}>
                  {reason}
                </div>
              ))
            ) : (
              <div className="reason-item">No decision reasons available yet.</div>
            )}
          </div>

          <div className="detail-row">
            <DetailPill
              label="Previous state"
              value={previousState ? describeBinary(previousState.is_on, "ON", "OFF") : "None"}
            />
            <DetailPill
              label="Last known plug"
              value={describeOptionalBinary(nextState.last_known_plug_is_on)}
            />
            <DetailPill
              label="Last actuation"
              value={nextState.last_actuation_at_iso ? formatDateTime(nextState.last_actuation_at_iso) : "Not recorded"}
            />
          </div>

          {nextState.last_actuation_error ? (
            <div className="controller-warning">
              <p className="section-caption">Last actuation error</p>
              <p>{nextState.last_actuation_error}</p>
            </div>
          ) : null}
        </Panel>

        <Panel
          title="Weather interpretation"
          kicker={weatherModeLabel}
          aside={weather.queried_timezone || "Timezone unavailable"}
        >
          <div className="weather-hero">
            <div>
              <p className="weather-temp">{formatTemp(weather.current_temperature_c)}</p>
              <p className="weather-caption">Current temperature</p>
            </div>
            <div className="weather-summary">
              <div className="weather-chip">{weatherCodeLabel}</div>
              <p>
                The controller currently identifies this as{" "}
                <strong>{weatherModeLabel.toLowerCase()}</strong>.
              </p>
            </div>
          </div>

          <div className="metric-grid">
            <DataTile label="Today low" value={formatTemp(weather.today_min_temperature_c)} />
            <DataTile label="Today high" value={formatTemp(weather.today_max_temperature_c)} />
            <DataTile label="Weather code" value={weather.weather_code ?? "N/A"} />
            <DataTile label="Timezone" value={weather.queried_timezone || "N/A"} />
          </div>
        </Panel>

        <Panel
          title="Manual override"
          kicker={override.is_active ? activeOverrideLabel : "Automatic mode"}
          aside={override.until_iso ? `Expires ${formatDateTime(override.until_iso)}` : "No timer running"}
        >
          <div className="override-meta">
            <StateBlock
              label="Override status"
              value={override.is_active ? "Active" : "Inactive"}
              detail={override.reason || describeOverrideWaiting(override)}
            />
            <StateBlock
              label="Release tracker"
              value={override.seen_auto_off ? "Auto OFF seen" : "Waiting"}
              detail="Used for the hold-off-until-auto-on mode."
            />
          </div>

          <label className="input-group" htmlFor="override-minutes">
            <span>Timer duration in minutes</span>
            <input
              id="override-minutes"
              type="number"
              min="1"
              step="1"
              value={overrideMinutes}
              onChange={(event) => {
                setOverrideMinutes(event.target.value);
              }}
            />
          </label>

          <div className="preset-row">
            {[30, 60, 120].map((minutes) => (
              <button
                key={minutes}
                className={`preset-chip ${overrideMinutes === String(minutes) ? "selected" : ""}`}
                type="button"
                onClick={() => {
                  setOverrideMinutes(String(minutes));
                }}
              >
                {minutes} min
              </button>
            ))}
          </div>

          <div className="action-grid">
            <button
              className="primary-button"
              type="button"
              onClick={() => {
                submitTimedOverride("on");
              }}
              disabled={pendingAction !== ""}
            >
              {pendingAction === "Manual ON override" ? "Applying..." : "Force ON"}
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={() => {
                submitTimedOverride("off");
              }}
              disabled={pendingAction !== ""}
            >
              {pendingAction === "Manual OFF override" ? "Applying..." : "Force OFF"}
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={() => {
                void runAction(
                  "Hold OFF until auto ON",
                  "/api/override/off-until-auto-on",
                  { method: "POST" },
                );
              }}
              disabled={pendingAction !== ""}
            >
              {pendingAction === "Hold OFF until auto ON" ? "Applying..." : "Stay OFF until auto ON"}
            </button>
            <button
              className="ghost-button"
              type="button"
              onClick={() => {
                void runAction("Clear override", "/api/override", { method: "DELETE" });
              }}
              disabled={pendingAction !== ""}
            >
              {pendingAction === "Clear override" ? "Clearing..." : "Return to automatic"}
            </button>
          </div>
        </Panel>
      </section>
    </main>
  );
}

function CountdownRing({ seconds, total }) {
  const radius = 9;
  const circumference = 2 * Math.PI * radius;
  const progress = Math.max(0, Math.min(1, seconds / total));
  const dashOffset = circumference * (1 - progress);

  return (
    <svg
      className="countdown-ring"
      width="22"
      height="22"
      viewBox="0 0 22 22"
    >
      <circle
        className="countdown-ring-track"
        cx="11"
        cy="11"
        r={radius}
        fill="none"
        strokeWidth="2"
      />
      <circle
        className="countdown-ring-fill"
        cx="11"
        cy="11"
        r={radius}
        fill="none"
        strokeWidth="2"
        strokeDasharray={circumference}
        strokeDashoffset={dashOffset}
        strokeLinecap="round"
        transform="rotate(-90 11 11)"
      />
    </svg>
  );
}

function Panel({ title, kicker, aside, children }) {
  return (
    <article className="glass-panel section-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">{kicker}</p>
          <h2>{title}</h2>
        </div>
        <p className="panel-aside">{aside}</p>
      </div>
      {children}
    </article>
  );
}

function MetricBadge({ label, value, detail, tone }) {
  return (
    <div className={`metric-badge tone-${tone}`}>
      <p>{label}</p>
      <strong>{value}</strong>
      <span>{detail}</span>
    </div>
  );
}

function DataTile({ label, value }) {
  return (
    <div className="data-tile">
      <p>{label}</p>
      <strong>{value}</strong>
    </div>
  );
}

function StateBlock({ label, value, detail }) {
  return (
    <div className="state-block">
      <p>{label}</p>
      <strong>{value}</strong>
      <span>{detail}</span>
    </div>
  );
}

function DetailPill({ label, value }) {
  return (
    <div className="detail-pill">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ProgressMeter({ label, value, tone }) {
  return (
    <div className="progress-meter">
      <div className="progress-meta">
        <span>{label}</span>
        <strong>{Math.round(value)}%</strong>
      </div>
      <div className="progress-track">
        <div
          className={`progress-fill tone-${tone}`}
          style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
        />
      </div>
    </div>
  );
}

function readJson(response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    return Promise.resolve(null);
  }
  return response.json();
}

function extractError(payload, fallbackMessage) {
  if (!payload) {
    return fallbackMessage;
  }
  if (typeof payload.detail === "string") {
    return payload.detail;
  }
  if (typeof payload.error === "string") {
    return payload.error;
  }
  return fallbackMessage;
}

function describeActionResult(label, payload) {
  if (payload?.override) {
    const overrideLabel = payload.override.is_active
      ? OVERRIDE_LABELS[payload.override.mode] ?? payload.override.mode
      : "Automatic mode restored";
    const actuationStatus = payload.actuation?.status ? ` (${payload.actuation.status})` : "";
    return `${overrideLabel}${actuationStatus}`;
  }
  if (payload?.actuation?.status) {
    return `${label} finished with ${payload.actuation.status}.`;
  }
  return `${label} completed.`;
}

function humanizeAction(action) {
  switch (action) {
    case "turn_on":
      return "Turn on";
    case "turn_off":
      return "Turn off";
    case "keep_on":
      return "Keep on";
    case "keep_off":
      return "Keep off";
    default:
      return action || "Awaiting controller data";
  }
}

function formatPercent(value) {
  if (value === null || value === undefined) {
    return "N/A";
  }
  return `${Number(value).toFixed(1)}%`;
}

function formatWatts(value) {
  if (value === null || value === undefined) {
    return "N/A";
  }
  return `${Math.round(Number(value)).toLocaleString()} W`;
}

function formatTemp(value) {
  if (value === null || value === undefined) {
    return "N/A";
  }
  return `${Number(value).toFixed(1)} C`;
}

function formatDateTime(value) {
  if (!value) {
    return "N/A";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function formatRelative(value) {
  if (!value) {
    return "just now";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "just now";
  }
  const diffMs = Date.now() - date.getTime();
  const diffSeconds = Math.max(0, Math.round(diffMs / 1000));
  if (diffSeconds < 5) {
    return "just now";
  }
  if (diffSeconds < 60) {
    return `${diffSeconds}s ago`;
  }
  const diffMinutes = Math.round(diffSeconds / 60);
  if (diffMinutes < 60) {
    return `${diffMinutes}m ago`;
  }
  return formatDateTime(value);
}

function describeBinary(value, truthyLabel, falsyLabel) {
  if (value === null || value === undefined) {
    return "Unknown";
  }
  return value ? truthyLabel : falsyLabel;
}

function describeOptionalBinary(value) {
  return describeBinary(value, "ON", "OFF");
}

function formatInputSource(value) {
  if (value === null || value === undefined) {
    return "Unavailable";
  }
  return `Source ${value}`;
}

function describeOverrideWaiting(override) {
  if (!override?.is_active) {
    return "No override is currently active.";
  }
  if (override.mode === "manual_off_until_next_auto_on") {
    return override.seen_auto_off
      ? "The controller has seen an automatic OFF and is waiting for the next fresh automatic ON."
      : "The controller is waiting to observe an automatic OFF before it can release on a future automatic ON.";
  }
  if (override.until_iso) {
    return `Timer ends ${formatDateTime(override.until_iso)}.`;
  }
  return "Override is active.";
}

function coercePercent(value) {
  if (value === null || value === undefined) {
    return 0;
  }
  return Math.max(0, Math.min(100, Number(value)));
}

function capPercent(value, ceiling) {
  if (value === null || value === undefined) {
    return 0;
  }
  const numeric = Number(value);
  return Math.max(0, Math.min(100, (numeric / ceiling) * 100));
}

function absValue(value) {
  if (value === null || value === undefined) {
    return 0;
  }
  return Math.abs(Number(value));
}

export default App;

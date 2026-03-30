# VRM Solar Automation

Python control scaffold for a Victron-driven circulation-pump automation. It runs on Windows today and is structured so the same code can later run on a Raspberry Pi.

## Current capabilities

The project now has two layers:

- `metrics`: fetches battery state of charge, solar production watts, and house load watts from the local Cerbo GX, using local MQTT as the preferred live feed and Modbus TCP as fallback
- `decide`: combines VRM state, Alaro weather, and remembered prior pump state to decide whether the circulation pump should be on or off
- `control`: evaluates the policy and reconciles the Shelly plug so the actual plug state matches the intended state whenever the plug is reachable
- `api`: exposes the controller state and override actions over FastAPI for a local frontend running on the same Raspberry Pi
- `override-*`: stores temporary manual overrides such as timed on/off and "stay off until the next fresh automatic on"
- `plug-*`: talks directly to a Shelly Gen2/Gen3 switch component so we can fetch plug info, inspect current output state, and issue on/off commands with optional delayed execution

The decision engine is designed around simple, explainable rules rather than opaque scoring.
The core data path is now local-first and LAN-only for power data, which is much better suited to on-site automation.

## Setup

Create a virtual environment if you do not already have one:

```powershell
py -3.13 -m venv .venv
```

Install dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

Install the React dashboard dependencies:

```powershell
cd frontend
npm install
cd ..
```

Add credentials to `.env`:

```dotenv
VICTRON_SITE_ID=123456
CERBO_HOST=192.168.68.66
CERBO_PORT=502
CERBO_SITE_NAME=Alaro (Cerbo GX)
CERBO_SITE_IDENTIFIER=cerbo-local
CERBO_MOCK_ENABLED=false
CERBO_MQTT_ENABLED=false
CERBO_MQTT_HOST=192.168.68.66
CERBO_MQTT_PORT=1883
CERBO_MQTT_USERNAME=
CERBO_MQTT_PASSWORD=
WEATHER_LATITUDE=39.707337
WEATHER_LONGITUDE=2.791675
WEATHER_TIMEZONE=Europe/Madrid
CONTROL_INTERVAL_SECONDS=30
TELEMETRY_STALE_AFTER_SECONDS=90
MODBUS_FALLBACK_POLL_SECONDS=30
POLICY_DEBOUNCE_MS=500
POLICY_MIN_RUN_INTERVAL_SECONDS=5
WEATHER_REFRESH_SECONDS=900
SHELLY_HOST=192.168.68.90
SHELLY_PORT=80
SHELLY_SWITCH_ID=0
SHELLY_USE_HTTPS=false
SHELLY_USERNAME=admin
SHELLY_PASSWORD=your-shelly-password
SHELLY_TIMEOUT_SECONDS=5.0
```

`VICTRON_SITE_ID` is now optional metadata only. The controller itself reads the energy values from the Cerbo GX over your local network.

If you are developing the frontend away from the Cerbo network, set `CERBO_MOCK_ENABLED=true` in `.env`. That swaps the live Modbus read for a fixed mock snapshot and lets the dashboard keep rendering realistic power data. To return to the real device later, set it back to `false` or remove the line.

## Live telemetry

The backend now keeps a shared live telemetry cache instead of probing the Cerbo on every `GET /api/status` request.

- Preferred path: Cerbo local MQTT updates feed the backend in near real time
- Fallback path: Modbus polling takes over when MQTT is disabled, disconnected, or stale
- Frontend path: FastAPI fans cached controller status to the dashboard over `GET /api/events`

Supported runtime matrix:

- Raspberry Pi / Linux with `CERBO_MQTT_ENABLED=true`: supported for live MQTT telemetry
- Native Windows with `CERBO_MQTT_ENABLED=false`: supported for Modbus fallback or mock telemetry
- Native Windows with `CERBO_MQTT_ENABLED=true`: not supported; backend startup now fails fast with an actionable error
- WSL on Windows with `CERBO_MQTT_ENABLED=true`: acceptable when you need live MQTT development on a Windows machine

To enable the preferred path on the Cerbo:

1. On the GX device, enable `Settings -> Integrations -> MQTT Access`.
2. Set `CERBO_MQTT_ENABLED=true` in `.env`.
3. Set `CERBO_SITE_IDENTIFIER` to the Cerbo VRM Portal ID, not the friendly site name, if you want the backend to send an immediate keepalive request and receive a full initial snapshot faster.

When MQTT is enabled, the backend still keeps Modbus available as a fallback. If no usable MQTT telemetry arrives for `TELEMETRY_STALE_AFTER_SECONDS`, it switches to Modbus fallback reads every `MODBUS_FALLBACK_POLL_SECONDS` until MQTT recovers.

## Run

Fetch raw energy metrics:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation metrics
```

Fetch metrics as JSON:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation metrics --json
```

Evaluate the pump policy:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation decide
```

Evaluate the pump policy as JSON:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation decide --json
```

Evaluate the policy and apply it to the Shelly plug:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation control
```

Evaluate the policy and apply it to the Shelly plug as JSON:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation control --json
```

Run the FastAPI backend:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation.api --host 0.0.0.0 --port 8000
```

Or via the installed script:

```powershell
vrm-api --host 0.0.0.0 --port 8000
```

When the FastAPI backend starts, it immediately launches the shared telemetry hub and cached controller coordinator. Automatic control now reacts to live telemetry with debounce and minimum-run throttling instead of relying only on a fixed request-time polling loop. `CONTROL_INTERVAL_SECONDS` remains available as compatibility metadata in the API payload and as a manual/fallback cadence indicator.

Build the dashboard for production so FastAPI can serve it from `/`:

```powershell
cd frontend
npm run build
cd ..
```

For local frontend development with live reload:

```powershell
cd frontend
npm run dev
```

The Vite dev server runs on `http://127.0.0.1:5173` and proxies `/api/*` to the FastAPI backend on `http://127.0.0.1:8000`.

Show the current temporary override:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation override-status
```

Force the pump on for 60 minutes:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation override-on --minutes 60
```

Force the pump off for 90 minutes:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation override-off --minutes 90
```

Keep the pump off until the controller sees the next fresh automatic ON signal:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation override-off-until-auto-on
```

Clear the temporary override and return to automatic control:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation override-clear
```

Minimal direct Cerbo test:

```powershell
.\.venv\Scripts\python.exe scripts\cerbo_modbus_snapshot.py
```

Fetch Shelly device info:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation plug-info
```

Check Shelly switch status:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation plug-status
```

Turn the plug on immediately:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation plug-on
```

Turn the plug on now and let the Shelly device auto-turn it off after 10 minutes:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation plug-on --toggle-after-seconds 600
```

Turn the plug off now and let the Shelly device auto-turn it back on after 30 seconds:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation plug-off --toggle-after-seconds 30
```

Run a minimal live test that turns the plug on and lets the Shelly device itself turn it off after 30 seconds:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation plug-test --on-seconds 30
```

## Decision strategy

The controller now follows a small deterministic flow:

1. Fail safe to `OFF` when battery SOC is unavailable.
2. Force `OFF` when generator power is `100 W` or more.
3. Use today's forecast only to determine demand:
   - `heating` when the daily low is `<= 12 C` and the high stays below `26 C`
   - `cooling` when the daily high is `>= 26 C` and the low stays above `12 C`
   - `mixed` when the forecast spans both sides of the comfort band
   - `mild` when the full day stays inside the comfort band
   - `unknown` when forecast min/max is unavailable
4. If demand exists, use battery hysteresis only:
   - turn `OFF` at or below `50%` battery
   - allow `ON` at or above `60%` battery
   - between `50%` and `60%`, keep the previous automatic state
   - if there is no previous automatic state in that band, default to `OFF`

The default policy values are:

- Protect a `50%` battery reserve
- Resume automatic runtime at `60%` battery
- Treat generator power of `100 W` or more as "generator on"
- Treat roughly `12 C` to `26 C` as the comfort band
- Do not use solar production as a decision input

These values live in [policy.py](C:\Users\fkots\visual_studio_code\vrm-solar-automation\src\vrm_solar_automation\policy.py) and are easy to tune once we observe real behavior.

## State handling

The controller stores the last automatic pump state, the last known Shelly state, and any temporary override in `.state/pump-policy-state.json`. That gives scheduled runs memory for hysteresis, reconciliation, and override release tracking.

## API Endpoints

The FastAPI backend is intended to sit beside a frontend on the same Raspberry Pi. The current minimal API includes:

- `GET /api/health`: simple health check plus control-loop and telemetry summary
- `GET /api/status`: latest cached controller status, including live telemetry metadata, current override state, automatic-loop status, and Shelly reachability/status
- `GET /api/events`: server-sent event stream of cached `status_update` payloads for the frontend
- `GET /api/plug/status`: direct Shelly switch status lookup
- `POST /api/control/run`: manually run the full control loop and reconcile the plug for maintenance or debugging
- `GET /api/override`: read the current temporary override
- `POST /api/override/on`: set a timed manual-on override
- `POST /api/override/off`: set a timed manual-off override
- `POST /api/override/off-until-auto-on`: keep the pump off until the next fresh automatic ON signal
- `POST /api/override/emergency-off`: hold the pump off until automatic control is manually restored
- `DELETE /api/override`: clear the override and return to automatic mode

Example timed override request body:

```json
{
  "minutes": 60
}
```

`GET /api/status` and `GET /api/events` now include a top-level `telemetry` object with:

- `transport`: `mqtt`, `modbus_fallback`, `mock`, or `unavailable`
- `connected`: whether the active telemetry transport is currently connected
- `fallback_active`: whether Modbus fallback is currently driving the cache
- `last_message_at_iso`: most recent telemetry update time
- `is_stale`: whether the current telemetry source is considered stale
- `stale_after_seconds`: the stale threshold in seconds
- `error`: latest transport-level error, if any

`GET /api/health`, `GET /api/status`, `POST /api/control/run`, and SSE `status_update` payloads also include a top-level `runtime` object that reports whether MQTT was requested and whether the current runtime supports it.

## Troubleshooting

- `add_reader()` / `add_writer()` `NotImplementedError` on Windows:
  Native Windows development does not support the current Cerbo MQTT runtime. Set `CERBO_MQTT_ENABLED=false`, use mock/Modbus on Windows, or run the backend in WSL or on the Raspberry Pi/Linux target.
- `Cerbo MQTT loop disconnected: Operation timed out`:
  The runtime started, but the configured MQTT endpoint did not complete a valid broker session. Check the Cerbo MQTT setting, tunnel/port forwarding, and broker authentication.
- `CERBO_SITE_IDENTIFIER` problems:
  The value must be the VRM Portal ID. Do not use the friendly site name such as `Alaro`.

## Frontend dashboard

The project now includes a small React dashboard in [`frontend`](C:\Users\fkots\visual_studio_code\vrm-solar-automation\frontend).

- Production flow: run `npm run build` in `frontend`, then start FastAPI and open `http://<host>:8000/`
- Development flow: run the FastAPI backend on port `8000`, then run `npm run dev` in `frontend`

The dashboard shows:

- battery, solar, house, and generator status
- controller decision state, remembered state, background-loop health, telemetry transport/freshness, and Shelly reachability
- identified weather mode plus current/min/max temperature data
- a sticky emergency-off switch plus manual override controls for timed ON, timed OFF, stay-OFF-until-auto-ON, and clear override

## Next step

The code now includes the full decision-and-actuation loop with a simplified automatic policy. The next tuning step is validating whether the `50%` reserve, `60%` restart threshold, and `12 C` to `26 C` comfort band match real-world behavior in your Alaro system.

## Python usage

```python
import asyncio

from vrm_solar_automation import ShellyPlugClient, ShellySettings


async def main() -> None:
    client = ShellyPlugClient(
        ShellySettings(
            host="192.168.68.90",
            switch_id=0,
            username="admin",
            password="your-shelly-password",
        )
    )

    await client.fetch_device_info()
    await client.turn_on()
    await client.turn_on_for(600)
    await client.turn_off_for(30)


asyncio.run(main())
```

`turn_on_for(seconds)` and `turn_off_for(seconds)` use Shelly's on-device `toggle_after` timer. Delayed state reversal is now handled only by the Shelly device rather than by Python sleeping and sending a second command later.

## Plug setup checklist

When you are on the same Wi-Fi as the Shelly plug, gather these values:

1. `SHELLY_HOST`: the plug's local IP or hostname. In the Shelly app or cloud UI, open the device settings and look for the device information page where the local IP is shown.
2. `SHELLY_SWITCH_ID`: for a single smart plug this is almost always `0`.
3. `SHELLY_USERNAME` and `SHELLY_PASSWORD`: only if you have enabled local web authentication on the device.
4. `SHELLY_USE_HTTPS`: usually `false` unless you specifically configured HTTPS on the device.

Then update `.env`, verify connectivity with:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation plug-info
.\.venv\Scripts\python.exe -m vrm_solar_automation plug-status
```

If those work, run the minimal relay test:

```powershell
.\.venv\Scripts\python.exe -m vrm_solar_automation plug-test --on-seconds 30
```

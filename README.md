# VRM Solar Automation

Python control scaffold for a Victron-driven circulation-pump automation. It runs on Windows today and is structured so the same code can later run on a Raspberry Pi.

## Current capabilities

The project now has two layers:

- `metrics`: fetches battery state of charge, solar production watts, and house load watts directly from the local Cerbo GX over Modbus TCP
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
WEATHER_LATITUDE=39.707337
WEATHER_LONGITUDE=2.791675
WEATHER_TIMEZONE=Europe/Madrid
CONTROL_INTERVAL_SECONDS=30
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

When the FastAPI backend starts, it immediately launches the automatic control loop and then keeps re-running it on the `CONTROL_INTERVAL_SECONDS` cadence. The dashboard is therefore monitoring and override UI, not a place to manually start the controller.

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

The current policy uses these ideas:

- Seasonal weather gating: use Alaro weather and the current month to distinguish likely heating days, cooling days, and mild days
- Battery safety hysteresis: turn off at a lower battery threshold and only resume at a higher one
- Solar assist: allow the pump to run earlier when solar generation is strong
- Generator blocking: keep the pump off while generator power is present
- Temporary overrides: allow timed manual on/off and a manual-off mode that only releases on the next fresh automatic ON cycle
- Intent-vs-actual reconciliation: remember the intended pump state and compare it to the Shelly's actual state on each control run
- Hold time: after a state change, keep that state for at least 20 minutes unless a hard safety condition applies

The default policy values are:

- Turn off below `55%` battery
- Resume above `72%` battery
- Allow solar-assisted operation above `65%` battery if solar is at least `2500 W`
- Treat generator power of `100 W` or more as "generator on"
- Treat roughly `12 C` to `26 C` as mild weather

These values live in [policy.py](C:\Users\fkots\visual_studio_code\vrm-solar-automation\src\vrm_solar_automation\policy.py) and are easy to tune once we observe real behavior.

## State handling

The controller stores the last automatic pump state, the last known Shelly state, and any temporary override in `.state/pump-policy-state.json`. That gives scheduled runs memory for hysteresis, reconciliation, and override release tracking.

## API Endpoints

The FastAPI backend is intended to sit beside a frontend on the same Raspberry Pi. The current minimal API includes:

- `GET /api/health`: simple health check
- `GET /api/status`: fresh read-only policy evaluation plus current override state, automatic-loop status, and current Shelly reachability/status
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

## Frontend dashboard

The project now includes a small React dashboard in [`frontend`](C:\Users\fkots\visual_studio_code\vrm-solar-automation\frontend).

- Production flow: run `npm run build` in `frontend`, then start FastAPI and open `http://<host>:8000/`
- Development flow: run the FastAPI backend on port `8000`, then run `npm run dev` in `frontend`

The dashboard shows:

- battery, solar, house, and generator status
- controller decision state, remembered state, background-loop health, and Shelly reachability
- identified weather mode plus current/min/max temperature data
- a sticky emergency-off switch plus manual override controls for timed ON, timed OFF, stay-OFF-until-auto-ON, and clear override

## Next step

The code now includes the full decision-and-actuation loop. The next tuning step is adjusting the thresholds with real-world observations from your Alaro system.

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

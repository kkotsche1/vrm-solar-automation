# VRM Solar Automation

Python automation for a Cerbo GX and Shelly-controlled circulation pump. The project now runs as a script-first controller: one Python process performs one control cycle, and a Raspberry Pi scheduler triggers that cycle every 30 seconds.

## Current capabilities

- `metrics`: fetch the current Cerbo GX power snapshot over Modbus TCP
- `decide`: evaluate the automatic pump policy without actuating the plug
- `control`: evaluate the policy and reconcile the Shelly plug whenever the observed reachable state diverges from the intended automatic target
- `db-upgrade`: apply Alembic migrations to create/update the automation database schema
- `plug-*`: inspect or manually control the Shelly plug directly
- `scripts/pump_control_snapshot.py`: run one controller cycle from a plain Python script for Raspberry Pi scheduling
- `scripts/pump_control_loop.py`: run controller cycles continuously in a fixed interval loop (default 15 seconds)
- `scripts/augment_historicals_weather.py`: enrich exported historical CSV files with per-day Open-Meteo archive weather columns

Email notifications are sent for:

- Shelly plug state changes initiated by the controller
- observed Shelly state mismatch versus the automatic target (sent once per mismatch episode until the states align again)
- battery SOC reaching each threshold in `BATTERY_ALERT_SOC_PERCENTS` (default `35%` and `25%`)
- generator power being detected at `100 W` or higher
- weather-based forecast blocks that keep automation `OFF` (sent at most once per weather-local day)

Notification emails are formatted for human readability: concise subjects, plain-language labels (no internal action tags), and bullet-list bodies with local date/time (`YYYY-MM-DD HH:MM`).

Battery, generator, and plug-mismatch alerts are latched in the database, so a one-shot scheduler only sends one alert per active condition. Each battery threshold sends exactly one email as SOC falls to it and stays latched while SOC keeps dropping; it re-arms only after SOC recovers above `threshold + BATTERY_ALERT_REARM_MARGIN_PERCENT`, so a discharge past several thresholds produces one email per threshold rather than a stream. The generator latch resets when generator power disappears, and the plug-mismatch latch resets when the observed plug state realigns with the automatic target. Weather-block alerts are date-latched and send once per `WEATHER_TIMEZONE` day.

Cerbo telemetry gaps never clear the battery or generator latches. An unreachable Cerbo leaves every latch exactly as it was, so recovering from a failed read does not re-send alerts that were already delivered for the ongoing discharge.

Manual override is no longer stored in the backend. If the plug is changed in the Shelly app and the controller can still reach the Shelly, the next control cycle reasserts the automatic target. If the Shelly is temporarily unreachable, the controller waits until it can read the plug state again before retrying alignment.

## Setup

Create a virtual environment:

```bash
python3 -m venv venv
```

Install the package:

```bash
venv/bin/activate
python -m pip install -e .
```

Create `.env` from `.env.example` and fill in your Cerbo GX and Shelly details.

Key settings:

```dotenv
CERBO_HOST=192.168.68.84
CERBO_PORT=502
CERBO_SITE_NAME=Alaro (Cerbo GX)
CERBO_SITE_IDENTIFIER=cerbo-local
CERBO_MOCK_ENABLED=false
CERBO_FETCH_RETRY_COUNT=2
CERBO_FETCH_RETRY_DELAY_SECONDS=1.0
CERBO_UNAVAILABLE_GRACE_CYCLES=10
WEATHER_LATITUDE=39.707337
WEATHER_LONGITUDE=2.791675
WEATHER_TIMEZONE=Europe/Madrid
SUNSHINE_HOURS_MIN=6.5
BATTERY_MIN_SOC_PERCENT=55
BATTERY_SOFT_MIN_SOC_PERCENT=35
BATTERY_HARD_MIN_SOC_PERCENT=22.5
BATTERY_CAPACITY_KWH=50
BATTERY_ALERT_SOC_PERCENTS=35,25
BATTERY_ALERT_REARM_MARGIN_PERCENT=5
AUTO_OFF_START_LOCAL=18:00
AUTO_RESUME_START_LOCAL=08:00
AUTO_CONTROL_TIMEZONE=Europe/Madrid
AUTO_RESUME_FOLLOWS_CROSSOVER=true
FORECAST_LIBERAL_SUNSHINE_HOURS_MIN=9.0
FORECAST_LIBERAL_SUNSHINE_HOURS_MAX=12.0
SURPLUS_NIGHT_ENABLED=true
SURPLUS_NIGHT_BASE_LOAD_KW=1.25
SURPLUS_NIGHT_MORNING_BASE_LOAD_KW=1.75
SURPLUS_NIGHT_MORNING_START_LOCAL=06:00
SURPLUS_NIGHT_CROSSOVER_AFTER_SUNRISE_MINUTES=60
SURPLUS_NIGHT_SOLAR_CROSSOVER_LOCAL=08:15
SURPLUS_NIGHT_GENERATOR_MARGIN_SOC_PERCENT=5.0
SURPLUS_NIGHT_PUMP_LOAD_KW=2.1
DAYTIME_SURPLUS_TURN_ON_ENABLED=true
DAYTIME_SURPLUS_MARGIN_KW=0.5
SURPLUS_NIGHT_MIN_RUN_HOURS=1.0
SURPLUS_NIGHT_TURN_ON_MARGIN_SOC_PERCENT=10
SURPLUS_NIGHT_MIN_TURN_ON_MARGIN_SOC_PERCENT=7
SURPLUS_NIGHT_NEXT_DAY_SUNSHINE_MIN=9.0
DATABASE_URL=sqlite:///.state/automation.db
DATABASE_AUTO_MIGRATE=false
SHELLY_HOST=192.168.68.90
SHELLY_PORT=80
SHELLY_SWITCH_ID=0
SHELLY_USE_HTTPS=false
SHELLY_USERNAME=admin
SHELLY_PASSWORD=your-shelly-password
SHELLY_TIMEOUT_SECONDS=5.0
SMTP_GMAIL_SENDER=kkotsche1@gmail.com
SMTP_GMAIL_APP_PASSWORD=your-gmail-app-password
SMTP_GMAIL_RECIPIENTS=f.kotschenreuther@yahoo.de,monika_kotschenreuther@yahoo.de,kkotsche1@gmail.com
```

Cerbo reads are now hardened against transient Modbus/TCP dropouts:

- `CERBO_FETCH_RETRY_COUNT` controls the number of retry attempts after the initial Cerbo read fails.
- `CERBO_FETCH_RETRY_DELAY_SECONDS` controls the fixed delay between retry attempts.
- `CERBO_UNAVAILABLE_GRACE_CYCLES` controls how many consecutive failed control cycles may preserve an already-running automatic `ON` target before the controller fails safe to `OFF`. Control cycles run every 30 s, so this is a wall-clock tolerance: `10` rides out roughly five minutes of Cerbo unreachability. Size it above the longest normal network dropout — observed Cerbo Wi-Fi dropouts reach ~130 s, so `3` (~60 s) tripped the fail-safe about once a day and cycled the pump for nothing.

Grace applies only to preserving an already-running automatic target. The controller never turns the pump on without a fresh successful Cerbo read.

`SUNSHINE_HOURS_MIN` configures the minimum daily direct-sunshine forecast required for automatic daytime demand. The controller requests `sunshine_duration` from Open-Meteo, converts it to hours, and only allows automatic demand when the forecast meets or exceeds this threshold. The initial production default for this installation is `6.5`.

The daytime SOC settings are now forecast-adaptive instead of using a single fixed cutoff:

- `BATTERY_MIN_SOC_PERCENT` is the conservative cloudy-day daytime threshold.
- `BATTERY_SOFT_MIN_SOC_PERCENT` is the preferred sunny-day daytime floor.
- `BATTERY_HARD_MIN_SOC_PERCENT` is the single non-negotiable floor and the daytime automatic cutoff. Reserve-aware night mode aims to land on this plus `SURPLUS_NIGHT_GENERATOR_MARGIN_SOC_PERCENT` at solar crossover.
- `FORECAST_LIBERAL_SUNSHINE_HOURS_MIN` and `FORECAST_LIBERAL_SUNSHINE_HOURS_MAX` define how quickly the controller interpolates from the conservative threshold toward the softer sunny-day thresholds.
- `BATTERY_CAPACITY_KWH` is used by reserve-aware night mode to convert base-load energy into an SOC reserve.

### Daytime solar-surplus override

The adaptive SOC thresholds are blind to production, which makes them wrong in exactly one situation: the morning handover. Reserve-aware night control is budgeted to land the battery on the night floor at solar crossover, and that floor sits below the daytime turn-on threshold, so on a clear morning the pump is locked out of the strongest sun of the day while the panels spill surplus.

When live solar covers the pump's own draw with `DAYTIME_SURPLUS_MARGIN_KW` to spare, the battery is not the energy source and the SOC gate is measuring the wrong thing, so it is bypassed for both turning on and keeping running. The pump draw is taken from `SURPLUS_NIGHT_PUMP_LOAD_KW` and is only subtracted when the pump is off, because `house_watts` already contains it while it runs.

Two limits still apply, because surplus can vanish behind a cloud and a start commits a compressor run that cannot be cancelled:

- SOC must be strictly above the night floor (`BATTERY_HARD_MIN_SOC_PERCENT + SURPLUS_NIGHT_GENERATOR_MARGIN_SOC_PERCENT`), so the override can never spend the reserve the night policy just protected.
- The sunshine-forecast gate (`SUNSHINE_HOURS_MIN`) and the hard floor are unaffected; the override only relaxes the adaptive SOC thresholds.

Set `DAYTIME_SURPLUS_TURN_ON_ENABLED=false` to restore pure SOC-gated daytime control.

There are exactly two control modes and no time-of-day bias within either one: the daytime adaptive policy, and reserve-aware night control. Daytime SOC thresholds depend only on the forecast, never on the clock.

`AUTO_OFF_START_LOCAL` and `AUTO_RESUME_START_LOCAL` define the overnight control window in `AUTO_CONTROL_TIMEZONE`. With `SURPLUS_NIGHT_ENABLED=true`, the controller switches to reserve-aware overnight automation instead of a hard forced-`OFF` quiet-hours block.

With `AUTO_RESUME_FOLLOWS_CROSSOVER=true` (the default) night control holds until the derived solar crossover instead of `AUTO_RESUME_START_LOCAL`, which then serves only as the fallback for when no sunrise is available. This matters because the two are not the same moment: resuming at a fixed time hands the last stretch of darkness to the daytime thresholds, whose turn-on can start the pump on charge the night reserve had earmarked for reaching crossover. Set it to `false` to restore the fixed-time boundary.
Seasonal `SUMMER_*` / `WINTER_*` quiet-hours keys are not supported.

The surplus-night settings keep the logic simple and deterministic:

- `SURPLUS_NIGHT_BASE_LOAD_KW` is the overnight house base load through the small hours, and `SURPLUS_NIGHT_MORNING_BASE_LOAD_KW` the heavier load from `SURPLUS_NIGHT_MORNING_START_LOCAL` until solar takes over.
- `SURPLUS_NIGHT_CROSSOVER_AFTER_SUNRISE_MINUTES` is how long after the forecast sunrise the panels are expected to start carrying the house. `SURPLUS_NIGHT_SOLAR_CROSSOVER_LOCAL` is the fixed fallback used only when no sunrise is available.
- `SURPLUS_NIGHT_GENERATOR_MARGIN_SOC_PERCENT` is headroom above the hard floor. The generator auto-starts on sagging DC voltage, so its effective SOC trip point rises with load. With the production values (`22.5` + `5.0`) the night reserve targets **27.5% SOC at solar crossover**.
- `SURPLUS_NIGHT_PUMP_LOAD_KW` is the average draw while the plug is on, and `SURPLUS_NIGHT_MIN_RUN_HOURS` the compressor run a start commits to, since it cannot be cancelled once triggered.
- `SURPLUS_NIGHT_TURN_ON_MARGIN_SOC_PERCENT` is the conservative night turn-on margin, pure hysteresis gating restarts only.
- `SURPLUS_NIGHT_MIN_TURN_ON_MARGIN_SOC_PERCENT` is the softened turn-on margin used on the strongest solar forecasts.
- `SURPLUS_NIGHT_NEXT_DAY_SUNSHINE_MIN` is the required sunshine-hours forecast for the next daylight period before night runtime is allowed.

The night reserve is `night floor + base-load energy still to be drawn before solar crossover`, where the night floor is `BATTERY_HARD_MIN_SOC_PERCENT + SURPLUS_NIGHT_GENERATOR_MARGIN_SOC_PERCENT`. Crossover is derived from the forecast sunrise rather than a clock time, because sunrise moves about two hours across the year here; it is *not* `AUTO_RESUME_START_LOCAL`, which is only when daytime control resumes.

With the production settings (`30 %` night floor, `46 kWh`, `1.25`/`1.75 kW`, crossover at sunrise + 60 min) the keep-running threshold runs `68.5 %` at `19:00`, `52.1 %` at `01:00`, `41.3 %` at `05:00` and `38.6 %` at `06:00` — it no longer decays onto the floor before dawn. Turning *on* additionally costs the committed compressor run, so a start is dearer than continuing.

`scripts/night_reserve_backtest.py` replays the logged nights through the old and new models, simulating each night forward from its 19:00 SOC using loads measured from the same capture. Use `--set FIELD=VALUE` to try a different tuning before deploying it.

The current site assumptions behind the reserve math are a `50 kWh` battery and a fixed `1.5 kW` overnight base load. The `4.5 kW` heat-pump draw and `15 kW` solar peak remain operational context for tuning, but they are not direct first-pass policy inputs yet.

`DATABASE_URL` points to the SQLAlchemy database connection used for runtime state and historical tracking. `DATABASE_AUTO_MIGRATE=true` can be enabled to run Alembic migrations automatically when the controller starts. For Raspberry Pi timer deployments, enable it or run `python -m vrm_solar_automation db-upgrade` before restarting the timer whenever the schema changes.

## Commands

Fetch raw Cerbo metrics:

```bash
python -m vrm_solar_automation metrics
```

Evaluate the policy without changing the plug:

```bash
python -m vrm_solar_automation decide
```

Run one full control cycle:

```bash
python -m vrm_solar_automation control
```

Upgrade database schema to latest revision:

```bash
python -m vrm_solar_automation db-upgrade
```

Run one full control cycle as JSON:

```bash
python -m vrm_solar_automation control --json
```

Run the Raspberry Pi script entrypoint:

```bash
python scripts/pump_control_snapshot.py --env-file .env
```

Run continuous control every 15 seconds in one long-lived process:

```bash
python scripts/pump_control_loop.py --env-file .env --interval-seconds 15
```

Augment all CSV files in `historicals/` with daily weather fields (three original header rows are preserved and weather columns are appended):

```bash
python scripts/augment_historicals_weather.py --env-file .env
```

By default this writes to `historicals/weather_augmented/`. Use `--in-place` to overwrite source files. The augmenter requests Open-Meteo archive data in day chunks (`--chunk-days`, default `31`) and applies exponential backoff plus `Retry-After` handling on `429`/transient server responses.

In long-running mode, weather is cached in memory per local weather day (`WEATHER_TIMEZONE`), so repeated cycles do not call Open-Meteo every interval. The cache resets when the process restarts.

Dry-run one cycle without actuation:

```bash
python scripts/pump_dry_run_snapshot.py --env-file .env
```

Check the Shelly status directly:

```bash
python -m vrm_solar_automation plug-status
```

Turn the plug on manually:

```bash
python -m vrm_solar_automation plug-on
```

Turn the plug off manually:

```bash
python -m vrm_solar_automation plug-off
```

## Raspberry Pi scheduling

The supported background model is external scheduling. The controller itself stays one-shot and exits after each cycle.

Recommended cadence: every 30 seconds.

Example `systemd` service:

```ini
[Unit]
Description=VRM Solar Automation control cycle
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=pi
Group=pi
WorkingDirectory=/home/pi/vrm-solar-automation
ExecStart=/home/pi/vrm-solar-automation/venv/bin/python /home/pi/vrm-solar-automation/scripts/pump_control_snapshot.py --env-file /home/pi/vrm-solar-automation/.env
```

Example `systemd` timer:

```ini
[Unit]
Description=Run VRM Solar Automation every 30 seconds

[Timer]
OnBootSec=30
OnUnitActiveSec=30
AccuracySec=1s
Unit=vrm-solar-automation.service

[Install]
WantedBy=timers.target
```

You can use `cron` instead if you prefer, but `systemd` timers are a better fit for 30-second scheduling.

Ready-to-copy unit files are included in:

- [deploy/systemd/vrm-solar-automation.service](/home/kotschi123/vrm-solar-automation/deploy/systemd/vrm-solar-automation.service)
- [deploy/systemd/vrm-solar-automation.timer](/home/kotschi123/vrm-solar-automation/deploy/systemd/vrm-solar-automation.timer)

Install them on the Raspberry Pi with:

```bash
sudo cp deploy/systemd/vrm-solar-automation.service /etc/systemd/system/
sudo cp deploy/systemd/vrm-solar-automation.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vrm-solar-automation.timer
```

If your Pi user or checkout path differs from `/home/pi/vrm-solar-automation`, edit `User`, `Group`, `WorkingDirectory`, and `ExecStart` in the service file before copying it into `/etc/systemd/system/`.

If you prefer a single long-running process, you can run:

```bash
python scripts/pump_control_loop.py --env-file .env --interval-seconds 15
```

## Decision strategy

The controller follows a small deterministic flow:

1. Fetch Cerbo telemetry with in-run retries (`CERBO_FETCH_RETRY_COUNT`, `CERBO_FETCH_RETRY_DELAY_SECONDS`).
2. If Cerbo telemetry is still unavailable after retries:
   - preserve an already-running automatic `ON` target through up to `CERBO_UNAVAILABLE_GRACE_CYCLES - 1` consecutive failed cycles
   - fail safe to `OFF` on the cycle that reaches `CERBO_UNAVAILABLE_GRACE_CYCLES`
   - never turn the pump `ON` without a fresh successful Cerbo read
3. Generator power does not gate the pump. Battery SOC is the only power-side turn-off trigger; generator power is reported and alerted on, but never forces `OFF` on its own.
4. Use the daily Open-Meteo sunshine forecast to determine daytime demand:
   - `sufficient_sun` when `today_sunshine_hours >= SUNSHINE_HOURS_MIN`
   - `insufficient_sun` when `today_sunshine_hours < SUNSHINE_HOURS_MIN`
   - `unknown` when sunshine hours are unavailable
   - when a live weather refresh fails, the one-shot controller reuses the last successful forecast for the same local weather day before falling back to `unknown`
5. If daytime demand exists, use forecast-adaptive SOC hysteresis:
   - always turn `OFF` at or below `BATTERY_HARD_MIN_SOC_PERCENT`
   - compute a forecast liberalization factor from `FORECAST_LIBERAL_SUNSHINE_HOURS_MIN` to `FORECAST_LIBERAL_SUNSHINE_HOURS_MAX`
   - keep the daytime demand gate based on `today_sunshine_hours`, but use the weaker of `today_sunshine_hours` and `tomorrow_sunshine_hours` to decide how conservative daytime SOC thresholds should be
   - use that factor to interpolate between `BATTERY_MIN_SOC_PERCENT` and the sunny-day thresholds derived from `BATTERY_SOFT_MIN_SOC_PERCENT`
   - these thresholds do not vary with time of day
   - override the turn-on and keep-running thresholds when live solar covers the pump with `DAYTIME_SURPLUS_MARGIN_KW` to spare and SOC is above the night floor
6. During the overnight window (`AUTO_OFF_START_LOCAL` to `AUTO_RESUME_START_LOCAL`):
   - if `SURPLUS_NIGHT_ENABLED=false`, the pump target is forced `OFF`
   - if `SURPLUS_NIGHT_ENABLED=true`, the controller switches to reserve-aware night mode
   - after the nightly start time, the night rule uses tomorrow's sunshine forecast; after midnight it uses today's sunshine forecast
   - the reserve is `BATTERY_HARD_MIN_SOC_PERCENT` plus the base-load energy still to be drawn before `AUTO_RESUME_START_LOCAL`, so it decays through the night
   - the pump keeps running down to exactly that reserve, landing the battery on `BATTERY_HARD_MIN_SOC_PERCENT` at resume time
   - turning `ON` additionally requires a hysteresis margin above the reserve, softened on stronger solar forecasts

## State handling

The controller uses a SQLite database (default: `.state/automation.db`) for persistence. It stores:

- current automatic target/runtime state in `controller_state` (single-row table)
- one historical record per `control` cycle in `control_cycle`

`control_cycle` rows include:

- Cerbo GX metrics (battery, solar, house, generator, active source, phase values)
- Cerbo telemetry status fields (`power_status_source`, `power_status_available`, `power_status_error`)
- weather snapshot used for policy evaluation, including `today_sunshine_hours`, `tomorrow_sunshine_hours`, plus `weather_source` (`live`, `same_day_cache`, or `unavailable`)
- policy decision fields (`should_turn_on`, `action`, `reason`, `weather_mode`, `soc_control_mode`)
- adaptive SOC diagnostics (`effective_turn_on_soc_percent`, `effective_turn_off_soc_percent`, `forecast_liberal_factor`)
- daytime solar-surplus diagnostics (`daytime_projected_surplus_kw`, `daytime_surplus_override_active`)
- reserve-aware night diagnostics (`night_required_soc_percent`, `night_surplus_mode_active`) when the overnight rule is active
- intended target and quiet-hours block metadata
- Shelly actuation result (`status`, command, observed before/after, error)

The runtime state still lets one-shot scheduling tolerate manual Shelly changes without introducing a separate override system. It also persists whether the previous cycle was quiet-hours-forced so a one-shot scheduler can turn the plug back on correctly when the quiet-hours window ends.

The same singleton runtime row also stores the alert latches: `battery_alert_latched_percents` (the comma-separated list of battery thresholds already alerted for the current discharge), the generator-running warning, the plug-mismatch warning, and the weather-block daily notification date.

The same singleton runtime row also stores the Cerbo telemetry failure streak plus the last Cerbo failure timestamp and error text. That is what lets the controller distinguish between a one-off transient read failure and a sustained outage before forcing the pump off.

The same singleton runtime row also caches the last successful weather snapshot for the local weather day. That cache is what prevents transient Open-Meteo failures from causing a same-minute `OFF` followed by `ON` when the next scheduled run succeeds again.

If you instantiate [StateStore](/home/kotschi123/vrm-solar-automation/src/vrm_solar_automation/state.py) directly in scripts or tests, call `close()` when finished or use it as a context manager so its SQLAlchemy engine is disposed cleanly.

## Database operations

Initial setup:

```bash
python -m vrm_solar_automation db-upgrade
```

After upgrading to versions that add alert/runtime fields (including the weather-block daily latch), run `db-upgrade` before restarting scheduled control cycles.

Backup:

```bash
cp .state/automation.db .state/automation.db.backup
```

Useful charting query examples:

```sql
SELECT timestamp_unix_ms, battery_soc_percent, solar_watts, generator_watts, house_watts
FROM control_cycle
WHERE timestamp_unix_ms BETWEEN ? AND ?
ORDER BY timestamp_unix_ms;
```

```sql
SELECT timestamp_unix_ms, actuation_status, actuation_command_sent
FROM control_cycle
WHERE actuation_command_sent IS NOT NULL
ORDER BY timestamp_unix_ms DESC
LIMIT 200;
```

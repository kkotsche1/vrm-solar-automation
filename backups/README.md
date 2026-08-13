# Archived control-cycle data

## `solar_automation_2026-07-11_to_2026-08-13_precontrolled.db`

29,220 control cycles, 2026-07-11 → 2026-08-13. Archived on 2026-08-13 when clean
capture was restarted. **Treat this dataset as unreliable for policy evaluation.**

### Why it is contaminated

1. **The plug was unreachable for all of July.** Every cycle from 2026-07-12 to
   2026-07-19 has `actuation_status='unreachable'` (22,531 rows total). The controller
   computed targets that never reached hardware, so `should_turn_on` in that period is
   an intention, not a record of what the pump did. `actuation_observed_*` is NULL
   throughout.
2. **The heat pump was deliberately run overnight.** Through most of July and part of
   August the heat pump was left running through the night on purpose, accepting
   generator starts. Overnight `house_watts` in that period (2.4–3.8 kW) therefore
   reflects a deliberate manual regime, not the normal base load (August, once normal,
   measured 0.78–0.99 kW).
3. **The generator ran on all 8 complete July nights** (26–47 % of each night), so SOC
   trajectories include recharges that mask what the battery alone would have done.

Only 2026-08-10 and 2026-08-12 are clean, complete, plug-reachable nights.

### What still holds

The **usable battery capacity estimate of ~23 kWh** survives the contamination. It was
derived as a load-to-decay *ratio* over 27 independent generator-free dark stretches, so
it does not depend on what the load was. July and August agreed independently (23.1 vs
23.3 kWh) across loads from 675 W to 2992 W. The configured `BATTERY_CAPACITY_KWH=50`
appears to overstate real usable capacity by ~2.2×.

This should be re-confirmed from the new capture using `battery_power_w`, which is
directly integrable and was not recorded in this archive.

### Known gaps in this archive

Not present (added in migration `20260813_0011`): `battery_power_w`,
`plug_observed_is_on`, and the `policy_*` fingerprint columns.

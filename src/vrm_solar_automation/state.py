from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .policy import PumpPolicyState


class StateStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def load(self) -> PumpPolicyState | None:
        if not self._path.exists():
            return None
        data = json.loads(self._path.read_text(encoding="utf-8"))
        return PumpPolicyState(
            is_on=bool(data["is_on"]),
            changed_at_iso=str(data["changed_at_iso"]),
            last_known_plug_is_on=(
                bool(data["last_known_plug_is_on"])
                if data.get("last_known_plug_is_on") is not None
                else None
            ),
            last_known_plug_at_iso=(
                str(data["last_known_plug_at_iso"])
                if data.get("last_known_plug_at_iso") is not None
                else None
            ),
            last_actuation_error=(
                str(data["last_actuation_error"])
                if data.get("last_actuation_error") is not None
                else None
            ),
            last_actuation_at_iso=(
                str(data["last_actuation_at_iso"])
                if data.get("last_actuation_at_iso") is not None
                else None
            ),
            override_mode=(
                str(data["override_mode"])
                if data.get("override_mode") is not None
                else None
            ),
            override_until_iso=(
                str(data["override_until_iso"])
                if data.get("override_until_iso") is not None
                else None
            ),
            override_set_at_iso=(
                str(data["override_set_at_iso"])
                if data.get("override_set_at_iso") is not None
                else None
            ),
            override_seen_auto_off=bool(data.get("override_seen_auto_off", False)),
        )

    def save(self, state: PumpPolicyState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(state.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def from_decision(previous_state: PumpPolicyState | None, should_turn_on: bool) -> PumpPolicyState:
        if previous_state and previous_state.is_on == should_turn_on:
            return previous_state
        last_known_plug_is_on = previous_state.last_known_plug_is_on if previous_state else None
        last_known_plug_at_iso = previous_state.last_known_plug_at_iso if previous_state else None
        last_actuation_error = previous_state.last_actuation_error if previous_state else None
        last_actuation_at_iso = previous_state.last_actuation_at_iso if previous_state else None
        override_mode = previous_state.override_mode if previous_state else None
        override_until_iso = previous_state.override_until_iso if previous_state else None
        override_set_at_iso = previous_state.override_set_at_iso if previous_state else None
        override_seen_auto_off = previous_state.override_seen_auto_off if previous_state else False
        return PumpPolicyState(
            is_on=should_turn_on,
            changed_at_iso=datetime.now(UTC).isoformat(),
            last_known_plug_is_on=last_known_plug_is_on,
            last_known_plug_at_iso=last_known_plug_at_iso,
            last_actuation_error=last_actuation_error,
            last_actuation_at_iso=last_actuation_at_iso,
            override_mode=override_mode,
            override_until_iso=override_until_iso,
            override_set_at_iso=override_set_at_iso,
            override_seen_auto_off=override_seen_auto_off,
        )

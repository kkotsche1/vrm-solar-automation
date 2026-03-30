from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from . import db
from .config import Settings, load_settings
from .runtime import RuntimeSupport, detect_runtime_support, ensure_supported_runtime
from .shelly import ShellyError, ShellyPlugClient
from .system import PumpControlSystem
from .telemetry import ControlCoordinator, TelemetryHub


class TimedOverrideRequest(BaseModel):
    minutes: float = Field(gt=0)


SettingsLoader = Callable[[str | Path], Settings]
SystemFactory = Callable[[Settings], PumpControlSystem]
PlugClientFactory = Callable[[Settings], ShellyPlugClient]

logger = logging.getLogger(__name__)


def _configure_windows_asyncio_policy() -> None:
    if os.name != "nt":
        return
    selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if selector_policy is None:
        return
    current_policy = asyncio.get_event_loop_policy()
    if not isinstance(current_policy, selector_policy):
        asyncio.set_event_loop_policy(selector_policy())


def _build_plug_client(settings: Settings) -> ShellyPlugClient:
    return ShellyPlugClient(settings.shelly_settings())


def create_app(
    env_file: str | Path | None = None,
    *,
    settings_loader: SettingsLoader = load_settings,
    system_factory: SystemFactory = PumpControlSystem,
    plug_client_factory: PlugClientFactory = _build_plug_client,
    frontend_dist: str | Path | None = None,
) -> FastAPI:
    resolved_env_file = str(env_file or os.environ.get("VRM_SOLAR_AUTOMATION_ENV_FILE", ".env"))

    def get_settings() -> Settings:
        try:
            return settings_loader(resolved_env_file)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        runtime_support = detect_runtime_support(settings)
        logger.info(
            "Runtime support: system=%s release=%s native_windows=%s mqtt_requested=%s mqtt_supported=%s",
            runtime_support.platform_system,
            runtime_support.platform_release,
            runtime_support.is_native_windows,
            runtime_support.mqtt_requested,
            runtime_support.mqtt_supported,
        )
        ensure_supported_runtime(settings, runtime_support)
        try:
            db.setup_database(settings.database_file)
        except Exception:
            logger.exception("Failed to setup metrics database")

        telemetry_hub = TelemetryHub(settings, runtime_support=runtime_support)
        coordinator = ControlCoordinator(
            settings,
            telemetry_hub,
            system_factory=system_factory,
            plug_client_factory=plug_client_factory,
        )

        app.state.settings = settings
        app.state.runtime_support = runtime_support
        app.state.telemetry_hub = telemetry_hub
        app.state.coordinator = coordinator
        app.state.sse_clients: set[asyncio.Queue] = set()
        coordinator.subscribe(lambda payload: _broadcast_sse(app, payload))

        await telemetry_hub.start()
        await coordinator.start()
        try:
            yield
        finally:
            await coordinator.stop()
            await telemetry_hub.stop()
            for queue in app.state.sse_clients:
                await queue.put(None)
            app.state.sse_clients.clear()

    app = FastAPI(
        title="VRM Solar Automation API",
        version="0.1.0",
        description="Minimal API for monitoring and steering the Shelly-backed pump controller.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        payload = await app.state.coordinator.get_status_payload()
        return {
            "ok": True,
            "control_loop": payload.get("control_loop", {}),
            "telemetry": payload.get("telemetry", {}),
            "runtime": _runtime_support_payload(app),
        }

    @app.get("/api/status")
    async def status() -> dict[str, object]:
        payload = await app.state.coordinator.get_status_payload()
        payload["runtime"] = _runtime_support_payload(app)
        return payload

    @app.get("/api/events")
    async def sse_events(request: Request) -> StreamingResponse:
        queue: asyncio.Queue = asyncio.Queue()
        app.state.sse_clients.add(queue)

        async def event_stream() -> AsyncGenerator[str, None]:
            last_keepalive_at = time.monotonic()
            try:
                initial_payload = await app.state.coordinator.get_status_payload()
                if initial_payload:
                    initial_payload = dict(initial_payload)
                    initial_payload["runtime"] = _runtime_support_payload(app)
                    yield f"event: status_update\ndata: {json.dumps(initial_payload)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        if time.monotonic() - last_keepalive_at >= 30.0:
                            yield ": keepalive\n\n"
                            last_keepalive_at = time.monotonic()
                        continue
                    if data is None:
                        break
                    yield f"event: status_update\ndata: {json.dumps(data)}\n\n"
            finally:
                app.state.sse_clients.discard(queue)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/plug/status")
    async def plug_status() -> dict[str, object]:
        return await _fetch_plug_status(app.state.settings, plug_client_factory)

    @app.post("/api/control/run")
    async def run_control() -> dict[str, object]:
        payload = await app.state.coordinator.run_manual_control()
        payload["runtime"] = _runtime_support_payload(app)
        return payload

    @app.get("/api/override")
    async def override_status() -> dict[str, object]:
        return system_factory(app.state.settings).read_override()

    @app.post("/api/override/on")
    async def override_on(request: TimedOverrideRequest) -> dict[str, object]:
        payload = await system_factory(app.state.settings).set_manual_on_override(
            duration_minutes=request.minutes,
        )
        await app.state.coordinator.refresh_status(broadcast=True)
        return payload

    @app.post("/api/override/off")
    async def override_off(request: TimedOverrideRequest) -> dict[str, object]:
        payload = await system_factory(app.state.settings).set_manual_off_override(
            duration_minutes=request.minutes,
        )
        await app.state.coordinator.refresh_status(broadcast=True)
        return payload

    @app.post("/api/override/off-until-auto-on")
    async def override_off_until_auto_on() -> dict[str, object]:
        payload = await system_factory(app.state.settings).set_manual_off_until_next_auto_on_override()
        await app.state.coordinator.refresh_status(broadcast=True)
        return payload

    @app.post("/api/override/emergency-off")
    async def override_emergency_off() -> dict[str, object]:
        payload = await system_factory(app.state.settings).set_emergency_off_override()
        await app.state.coordinator.refresh_status(broadcast=True)
        return payload

    @app.delete("/api/override")
    async def clear_override() -> dict[str, object]:
        payload = await system_factory(app.state.settings).clear_override()
        await app.state.coordinator.refresh_status(broadcast=True)
        return payload

    _configure_frontend_routes(app, frontend_dist)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the FastAPI backend for VRM Solar Automation.",
    )
    parser.add_argument(
        "--env-file",
        default=os.environ.get("VRM_SOLAR_AUTOMATION_ENV_FILE", ".env"),
        help="Path to the .env file for the controller.",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host interface to bind to.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="TCP port to bind to.",
    )
    args = parser.parse_args()
    uvicorn.run(
        create_app(args.env_file),
        host=args.host,
        port=args.port,
    )


async def _fetch_plug_status(
    settings: Settings,
    plug_client_factory: PlugClientFactory,
) -> dict[str, Any]:
    if not settings.shelly_host:
        return {
            "configured": False,
            "reachable": False,
            "error": "SHELLY_HOST is not configured.",
            "status": None,
        }

    try:
        status = await plug_client_factory(settings).fetch_switch_status()
        return {
            "configured": True,
            "reachable": True,
            "error": None,
            "status": status.to_dict(),
        }
    except ShellyError as exc:
        return {
            "configured": True,
            "reachable": False,
            "error": str(exc),
            "status": None,
        }


def _broadcast_sse(app: FastAPI, payload: dict[str, object]) -> None:
    payload = dict(payload)
    payload["runtime"] = _runtime_support_payload(app)
    clients: set[asyncio.Queue] = getattr(app.state, "sse_clients", set())
    for queue in clients:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def _runtime_support_payload(app: FastAPI) -> dict[str, object] | None:
    support: RuntimeSupport | None = getattr(app.state, "runtime_support", None)
    if support is None:
        return None
    return support.to_dict()


def _configure_frontend_routes(app: FastAPI, frontend_dist: str | Path | None) -> None:
    dist_dir = _resolve_frontend_dist(frontend_dist)
    if dist_dir is None:
        return

    @app.get("/", include_in_schema=False)
    async def frontend_index() -> FileResponse:
        return FileResponse(dist_dir / "index.html")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend_asset(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        return _frontend_response(dist_dir, path)


def _frontend_response(dist_dir: Path, path: str) -> FileResponse:
    normalized_path = Path(path)
    requested_path = (dist_dir / normalized_path).resolve()
    if not requested_path.is_relative_to(dist_dir):
        raise HTTPException(status_code=404, detail="Not Found")
    if requested_path.is_file():
        return FileResponse(requested_path)
    return FileResponse(dist_dir / "index.html")


def _resolve_frontend_dist(frontend_dist: str | Path | None) -> Path | None:
    candidate = Path(frontend_dist) if frontend_dist is not None else _default_frontend_dist()
    candidate = candidate.resolve()
    index_file = candidate / "index.html"
    if not index_file.exists():
        return None
    return candidate


def _default_frontend_dist() -> Path:
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


_configure_windows_asyncio_policy()
app = create_app()


if __name__ == "__main__":
    main()

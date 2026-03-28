from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
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
from .shelly import ShellyError, ShellyPlugClient
from .system import PumpControlSystem


class TimedOverrideRequest(BaseModel):
    minutes: float = Field(gt=0)


SettingsLoader = Callable[[str | Path], Settings]
SystemFactory = Callable[[Settings], PumpControlSystem]
PlugClientFactory = Callable[[Settings], ShellyPlugClient]

logger = logging.getLogger(__name__)


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
        try:
            db.setup_database(get_settings().database_file)
        except Exception:
            logger.exception("Failed to setup metrics database")
            
        app.state.control_loop = _build_control_loop_snapshot()
        app.state.sse_clients: set[asyncio.Queue] = set()
        await _run_control_loop_iteration(app, get_settings, system_factory, plug_client_factory)
        task = asyncio.create_task(_run_control_loop(app, get_settings, system_factory, plug_client_factory))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            app.state.control_loop["is_active"] = False
            app.state.control_loop["is_iteration_in_progress"] = False
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
        return {"ok": True, "control_loop": _control_loop_snapshot(app)}

    @app.get("/api/status")
    async def status() -> dict[str, object]:
        settings = get_settings()
        system = system_factory(settings)
        decision, payload = await system.evaluate()
        payload["decision"] = decision.to_dict()
        payload["plug"] = await _fetch_plug_status(settings, plug_client_factory)
        snapshot = _control_loop_snapshot(app)
        snapshot["interval_seconds"] = settings.control_interval_seconds
        payload["control_loop"] = snapshot
        return payload

    @app.get("/api/events")
    async def sse_events(request: Request) -> StreamingResponse:
        queue: asyncio.Queue = asyncio.Queue()
        app.state.sse_clients.add(queue)

        async def event_stream() -> AsyncGenerator[str, None]:
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
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
        return await _fetch_plug_status(get_settings(), plug_client_factory)

    @app.post("/api/control/run")
    async def run_control() -> dict[str, object]:
        settings = get_settings()
        system = system_factory(settings)
        _, payload = await system.control()
        payload["plug"] = await _fetch_plug_status(settings, plug_client_factory)
        payload["control_loop"] = _control_loop_snapshot(app)
        return payload

    @app.get("/api/override")
    async def override_status() -> dict[str, object]:
        settings = get_settings()
        return system_factory(settings).read_override()

    @app.post("/api/override/on")
    async def override_on(request: TimedOverrideRequest) -> dict[str, object]:
        settings = get_settings()
        return await system_factory(settings).set_manual_on_override(
            duration_minutes=request.minutes,
        )

    @app.post("/api/override/off")
    async def override_off(request: TimedOverrideRequest) -> dict[str, object]:
        settings = get_settings()
        return await system_factory(settings).set_manual_off_override(
            duration_minutes=request.minutes,
        )

    @app.post("/api/override/off-until-auto-on")
    async def override_off_until_auto_on() -> dict[str, object]:
        settings = get_settings()
        return await system_factory(settings).set_manual_off_until_next_auto_on_override()

    @app.post("/api/override/emergency-off")
    async def override_emergency_off() -> dict[str, object]:
        settings = get_settings()
        return await system_factory(settings).set_emergency_off_override()

    @app.delete("/api/override")
    async def clear_override() -> dict[str, object]:
        settings = get_settings()
        return await system_factory(settings).clear_override()

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


def _build_control_loop_snapshot() -> dict[str, object]:
    return {
        "is_active": False,
        "is_iteration_in_progress": False,
        "interval_seconds": None,
        "last_started_at_iso": None,
        "last_completed_at_iso": None,
        "last_actuation_status": None,
        "last_error": None,
    }


def _control_loop_snapshot(app: FastAPI) -> dict[str, object]:
    snapshot = getattr(app.state, "control_loop", None)
    if snapshot is None:
        return _build_control_loop_snapshot()
    return dict(snapshot)


async def _run_control_loop(
    app: FastAPI,
    get_settings: Callable[[], Settings],
    system_factory: SystemFactory,
    plug_client_factory: PlugClientFactory,
) -> None:
    snapshot = app.state.control_loop
    snapshot["is_active"] = True

    while True:
        interval_seconds = snapshot["interval_seconds"] or 60.0
        await asyncio.sleep(interval_seconds)
        await _run_control_loop_iteration(app, get_settings, system_factory, plug_client_factory)


async def _run_control_loop_iteration(
    app: FastAPI,
    get_settings: Callable[[], Settings],
    system_factory: SystemFactory,
    plug_client_factory: PlugClientFactory,
) -> None:
    snapshot = app.state.control_loop
    snapshot["is_active"] = True
    snapshot["is_iteration_in_progress"] = True
    snapshot["last_started_at_iso"] = datetime.now(UTC).isoformat()

    try:
        settings = get_settings()
        snapshot["interval_seconds"] = max(1.0, float(settings.control_interval_seconds))
        system = system_factory(settings)
        _, payload = await system.control()
        snapshot["last_actuation_status"] = payload.get("actuation", {}).get("status")
        snapshot["last_error"] = None

        # Build a full status-like payload and push to SSE clients
        try:
            plug_data = await _fetch_plug_status(settings, plug_client_factory)
        except Exception:
            plug_data = {"configured": False, "reachable": False, "error": "unavailable", "status": None}
        loop_snapshot = dict(snapshot)
        loop_snapshot["interval_seconds"] = settings.control_interval_seconds
        payload["plug"] = plug_data
        payload["control_loop"] = loop_snapshot
        _broadcast_sse(app, payload)

        try:
            await asyncio.to_thread(db.insert_metrics, settings.database_file, payload)
        except Exception:
            logger.exception("Failed to save metrics to database")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        snapshot["last_error"] = _describe_exception(exc)
        logger.exception("Automatic control loop iteration failed.")
    finally:
        snapshot["last_completed_at_iso"] = datetime.now(UTC).isoformat()
        snapshot["is_iteration_in_progress"] = False


def _broadcast_sse(app: FastAPI, payload: dict[str, object]) -> None:
    clients: set[asyncio.Queue] = getattr(app.state, "sse_clients", set())
    for queue in clients:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def _describe_exception(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str) and detail:
            return detail
    return str(exc) or exc.__class__.__name__


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


app = create_app()


if __name__ == "__main__":
    main()

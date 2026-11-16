from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import service
from .db import Database
from .schemas import ResolveRequest, TopologyIn

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_DB_PATH = os.environ.get(
    "DATABASE_PATH", str(PROJECT_DIR / "data" / "rehearsal.db")
)


def _error_payload(code: str, message: str, details: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
        message = item.get("msg", "invalid value")
        for prefix in ("Value error, ", "Assertion failed, "):
            if message.startswith(prefix):
                message = message[len(prefix):]
        details.append(
            {
                "field": location or "body",
                "message": message,
                "type": item.get("type", "value_error"),
            }
        )
    return details


def _seed_if_empty(db: Database, path: str | Path) -> None:
    db.initialize()
    state = service.snapshot(db)
    if state["topology_loaded"]:
        return
    seed_path = Path(path)
    if not seed_path.is_file():
        return
    try:
        payload = json.loads(seed_path.read_text(encoding="utf-8"))
        topology = TopologyIn.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - seed is best effort, log via stdout
        print(f"[seed] skipped invalid topology {seed_path}: {exc}")
        return
    service.import_topology(db, topology)
    print(f"[seed] loaded topology from {seed_path}")


def create_app(
    db_path: str | Path | None = None,
    *,
    seed_path: str | Path | None = None,
) -> FastAPI:
    database = Database(db_path or DEFAULT_DB_PATH)
    seed = seed_path if seed_path is not None else os.environ.get("SEED_TOPOLOGY")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        if seed:
            _seed_if_empty(database, seed)
        service.reconcile(database)
        yield

    app = FastAPI(
        title="节点排空演练模拟器",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.db = database

    @app.exception_handler(service.ApiError)
    async def api_error_handler(_: Request, exc: service.ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(exc.code, exc.message),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if request.url.path == "/api/topology":
            code = "INVALID_TOPOLOGY"
            message = "导入数据校验失败，请检查字段、数量上限和容量约束"
        else:
            code = "INVALID_REQUEST"
            message = "请求参数校验失败"
        return JSONResponse(
            status_code=422,
            content=_error_payload(code, message, _validation_details(exc)),
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/state")
    def get_state() -> dict[str, Any]:
        return service.snapshot(database)

    @app.post("/api/topology")
    def import_topology(
        topology: TopologyIn = Body(...),
    ) -> dict[str, Any]:
        return service.import_topology(database, topology)

    @app.get("/api/topology/example")
    def example_topology() -> JSONResponse:
        """The demo topology served to the UI's "load sample" button."""
        example = PROJECT_DIR / "examples" / "cluster.json"
        if not example.is_file():
            raise service.ApiError(404, "EXAMPLE_NOT_FOUND", "example topology is missing")
        return JSONResponse(json.loads(example.read_text(encoding="utf-8")))

    @app.post("/api/reset")
    def reset() -> dict[str, Any]:
        return service.reset(database)

    @app.post("/api/nodes/{node_id}/drain")
    def start_drain(node_id: str) -> dict[str, Any]:
        return service.start_drain(database, node_id)

    @app.post("/api/nodes/{node_id}/resume")
    def resume_drain(node_id: str) -> dict[str, Any]:
        return service.resume_drain(database, node_id)

    @app.post("/api/nodes/{node_id}/restore")
    def restore_node(node_id: str) -> dict[str, Any]:
        return service.restore_node(database, node_id)

    @app.post("/api/migrations/{migration_id}/resolve")
    def resolve_migration(
        migration_id: int, request: ResolveRequest
    ) -> dict[str, Any]:
        return service.resolve_migration(
            database, migration_id, request.outcome, request.reason
        )

    @app.get("/api/drains/{drain_id}")
    def get_drain(drain_id: int) -> dict[str, Any]:
        return service.get_drain(database, drain_id)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()

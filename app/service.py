"""Domain logic for node draining with reserve-then-cutover migrations.

Invariants enforced inside every ``BEGIN IMMEDIATE`` SQLite transaction:

* Occupied CPU on a node = CPU of ``serving`` containers placed there +
  CPU reserved by ``starting`` new replicas targeting that node. The old
  replica keeps serving while its replacement starts, so both are counted.
* A drain reserves capacity on a target *before* any replica is promoted, and
  the old replica is only removed in the same transaction that promotes the
  new replica. A service's ready count therefore never dips below
  ``min_ready``.
* If there is no viable target, or the simulated start fails, the reservation
  is released and the old replica keeps serving; the container is reported as
  blocked.
* Every mutation re-reads state under the write lock, so concurrent drains
  cannot double-book capacity. Nothing is rebuilt in memory at startup: an
  interrupted drain resumes from the persisted SQLite state.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .schemas import TopologyIn

OPEN_DRAIN_STATUSES = ("active", "incomplete")
BLOCKED_STATES = ("failed", "blocked")
MAX_HISTORY_DRAINS = 12


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Shared queries
# --------------------------------------------------------------------------- #


def _node_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM nodes ORDER BY position, id").fetchall()


def _service_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM services ORDER BY position, name").fetchall()


def _serving_containers(
    conn: sqlite3.Connection, node_id: str | None = None
) -> list[sqlite3.Row]:
    if node_id is None:
        return conn.execute(
            "SELECT * FROM containers WHERE status = 'serving' ORDER BY position, id"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM containers WHERE node_id = ? AND status = 'serving' "
        "ORDER BY position, id",
        (node_id,),
    ).fetchall()


def _starting_migrations(
    conn: sqlite3.Connection, target_node_id: str | None = None
) -> list[sqlite3.Row]:
    if target_node_id is None:
        return conn.execute(
            "SELECT * FROM migrations WHERE state = 'starting' ORDER BY id"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM migrations WHERE state = 'starting' AND target_node_id = ? "
        "ORDER BY id",
        (target_node_id,),
    ).fetchall()


def _usage(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """CPU accounting per node: serving requests plus in-flight reservations."""
    usage: dict[str, dict[str, int]] = {
        node["id"]: {"serving": 0, "reserved": 0} for node in _node_rows(conn)
    }
    for row in conn.execute(
        "SELECT node_id, SUM(cpu_request) AS total FROM containers "
        "WHERE status = 'serving' GROUP BY node_id"
    ).fetchall():
        usage.setdefault(row["node_id"], {"serving": 0, "reserved": 0})[
            "serving"
        ] = row["total"]
    for row in conn.execute(
        "SELECT target_node_id, SUM(reserved_cpu) AS total FROM migrations "
        "WHERE state = 'starting' AND target_node_id IS NOT NULL "
        "GROUP BY target_node_id"
    ).fetchall():
        usage.setdefault(row["target_node_id"], {"serving": 0, "reserved": 0})[
            "reserved"
        ] = row["total"]
    return usage


def _open_drains(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM drains WHERE status IN ('active','incomplete') ORDER BY id"
    ).fetchall()


def _open_drain_for_node(
    conn: sqlite3.Connection, node_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM drains WHERE node_id = ? AND status IN ('active','incomplete') "
        "ORDER BY id DESC LIMIT 1",
        (node_id,),
    ).fetchone()


def _draining_node_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        row["node_id"]
        for row in conn.execute(
            "SELECT node_id FROM drains WHERE status IN ('active','incomplete')"
        ).fetchall()
    }


def _attempts(conn: sqlite3.Connection, drain_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM migrations WHERE drain_id = ? ORDER BY id", (drain_id,)
    ).fetchall()


def _latest_attempts(
    conn: sqlite3.Connection, drain_id: int
) -> dict[str, sqlite3.Row]:
    latest: dict[str, sqlite3.Row] = {}
    for row in _attempts(conn, drain_id):
        latest[row["container_id"]] = row
    return latest


def _pending_migrations(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        row["container_id"]: row
        for row in conn.execute(
            "SELECT * FROM migrations WHERE state = 'starting' ORDER BY id"
        ).fetchall()
    }


# --------------------------------------------------------------------------- #
# Read model
# --------------------------------------------------------------------------- #


def _public_migration(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "drain_id": row["drain_id"],
        "container_id": row["container_id"],
        "source_node_id": row["source_node_id"],
        "target_node_id": row["target_node_id"],
        "state": row["state"],
        "reserved_cpu": row["reserved_cpu"],
        "error_code": row["error_code"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _drain_public(
    conn: sqlite3.Connection,
    drain: sqlite3.Row,
) -> dict[str, Any]:
    node_id = drain["node_id"]
    node = conn.execute("SELECT name FROM nodes WHERE id = ?", (node_id,)).fetchone()
    attempts = _attempts(conn, drain["id"])
    latest = _latest_attempts(conn, drain["id"])
    serving = _serving_containers(conn, node_id)

    # Progress is measured against every logical container this drain has ever
    # tried to move, plus anything still serving on the node. Unfinished
    # replicas therefore never inflate the number.
    tracked_ids: set[str] = set(latest)
    tracked_ids.update(container["id"] for container in serving)
    total = len(tracked_ids)
    # A completed attempt only counts once its replica actually left this
    # node; a later drain may have placed it back.
    placed_elsewhere = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM containers WHERE node_id != ?", (node_id,)
        ).fetchall()
    }
    moved = sum(
        1
        for row in latest.values()
        if row["state"] == "completed" and row["container_id"] in placed_elsewhere
    )
    if drain["status"] == "completed":
        # History is frozen at completion; later drains moving replicas back
        # must not rewrite an old drain's result.
        moved = total
    pending = [row for row in attempts if row["state"] == "starting"]
    failed = [row for row in latest.values() if row["state"] == "failed"]
    blocked = [row for row in latest.values() if row["state"] == "blocked"]

    return {
        "id": drain["id"],
        "node_id": node_id,
        "node_name": node["name"] if node else node_id,
        "status": drain["status"],
        "created_at": drain["created_at"],
        "finished_at": drain["finished_at"],
        "total": total,
        "moved": moved,
        "remaining_on_node": len(serving),
        "starting": len(pending),
        "failed": len(failed),
        "blocked": len(blocked),
        "progress_percent": round((moved / total) * 100, 1) if total else 100.0,
        "pending_decisions": [_public_migration(row) for row in pending],
        "attempts": [_public_migration(row) for row in attempts],
    }


def snapshot(db: Database) -> dict[str, Any]:
    """Whole-cluster read model. Never mutates state."""
    with db.read() as conn:
        nodes = _node_rows(conn)
        services = _service_rows(conn)
        containers = conn.execute(
            "SELECT * FROM containers ORDER BY position, id"
        ).fetchall()
        container_lookup = {row["id"]: row for row in containers}
        usage = _usage(conn)
        open_drains = _open_drains(conn)
        open_by_node = {drain["node_id"]: drain for drain in open_drains}
        drain_public = {
            drain["id"]: _drain_public(conn, drain)
            for drain in open_drains
        }
        pending = _pending_migrations(conn)

        ready_by_service: dict[str, int] = {}
        for container in containers:
            if container["status"] == "serving":
                ready_by_service[container["service"]] = (
                    ready_by_service.get(container["service"], 0) + 1
                )

        blocked_containers: list[dict[str, Any]] = []
        for drain in open_drains:
            for row in _latest_attempts(conn, drain["id"]).values():
                if row["state"] not in BLOCKED_STATES:
                    continue
                container = container_lookup.get(row["container_id"])
                if container is None or container["node_id"] != drain["node_id"]:
                    continue
                blocked_containers.append(
                    {
                        "container_id": container["id"],
                        "name": container["name"],
                        "service": container["service"],
                        "cpu_request": container["cpu_request"],
                        "node_id": container["node_id"],
                        "drain_id": drain["id"],
                        "drain_node_id": drain["node_id"],
                        "attempt_id": row["id"],
                        "target_node_id": row["target_node_id"],
                        "reason_code": row["error_code"] or "INSUFFICIENT_CAPACITY",
                        "reason": row["error"] or "no viable target",
                        "updated_at": row["updated_at"],
                        "retryable": drain["status"] == "incomplete",
                    }
                )

        node_payload: list[dict[str, Any]] = []
        for node in nodes:
            row = usage.get(node["id"], {"serving": 0, "reserved": 0})
            drain = open_by_node.get(node["id"])
            inbound = _starting_migrations(conn, node["id"])
            serving_here = [
                container
                for container in containers
                if container["node_id"] == node["id"]
                and container["status"] == "serving"
            ]
            last_completed = conn.execute(
                "SELECT * FROM drains WHERE node_id = ? AND status = 'completed' "
                "ORDER BY id DESC LIMIT 1",
                (node["id"],),
            ).fetchone()
            node_payload.append(
                {
                    "id": node["id"],
                    "name": node["name"],
                    "cpu_capacity": node["cpu_capacity"],
                    "used_cpu": row["serving"] + row["reserved"],
                    "serving_cpu": row["serving"],
                    "reserved_cpu": row["reserved"],
                    "remaining_cpu": max(
                        node["cpu_capacity"] - row["serving"] - row["reserved"], 0
                    ),
                    "status": node["status"],
                    "serving_containers": len(serving_here),
                    "inbound_reservations": [
                        {
                            "migration_id": item["id"],
                            "container_id": item["container_id"],
                            "cpu": item["reserved_cpu"],
                            "drain_id": item["drain_id"],
                        }
                        for item in inbound
                    ],
                    "drain": drain_public[drain["id"]] if drain else None,
                    "last_completed_drain": (
                        _public_drain_row(last_completed)
                        if last_completed is not None and drain is None
                        else None
                    ),
                    "can_drain": (
                        node["status"] == "ready"
                        and drain is None
                        and not inbound
                    ),
                    "can_resume": (
                        drain is not None
                        and drain["status"] == "incomplete"
                        and not inbound
                    ),
                    "can_restore": (
                        node["status"] == "drained"
                        and not serving_here
                        and not row["reserved"]
                        and not inbound
                        and drain is None
                    ),
                    # Derived, never stored: a node only counts as empty with
                    # zero serving replicas and zero reservations on it.
                    "is_empty": (
                        node["status"] == "drained"
                        and not serving_here
                        and not row["reserved"]
                        and not inbound
                    ),
                }
            )

        container_payload: list[dict[str, Any]] = []
        for container in containers:
            current_drain = open_by_node.get(container["node_id"])
            last_attempt = None
            if current_drain is not None:
                last_attempt = _latest_attempts(conn, current_drain["id"]).get(
                    container["id"]
                )
                if last_attempt is not None and last_attempt["state"] not in (
                    BLOCKED_STATES
                ):
                    last_attempt = None
            container_payload.append(
                {
                    "id": container["id"],
                    "name": container["name"],
                    "service": container["service"],
                    "cpu_request": container["cpu_request"],
                    "node_id": container["node_id"],
                    "status": container["status"],
                    "migration": (
                        _public_migration(pending[container["id"]])
                        if container["id"] in pending
                        else None
                    ),
                    "blocked_attempt": (
                        _public_migration(last_attempt)
                        if last_attempt is not None
                        else None
                    ),
                }
            )

        service_payload: list[dict[str, Any]] = []
        for service in services:
            ready = ready_by_service.get(service["name"], 0)
            starting = sum(
                1
                for container_id in pending
                if container_lookup[container_id]["service"] == service["name"]
            )
            defined = sum(
                1 for container in containers if container["service"] == service["name"]
            )
            service_payload.append(
                {
                    "name": service["name"],
                    "min_ready": service["min_ready"],
                    "ready": ready,
                    "defined": defined,
                    "starting_replicas": starting,
                    "healthy": ready >= service["min_ready"],
                }
            )

        history = conn.execute(
            "SELECT * FROM drains ORDER BY id DESC LIMIT ?", (MAX_HISTORY_DRAINS,)
        ).fetchall()

        return {
            "topology_loaded": bool(nodes),
            "nodes": node_payload,
            "containers": container_payload,
            "services": service_payload,
            "blocked_containers": blocked_containers,
            "open_drains": [drain_public[drain["id"]] for drain in open_drains],
            "drains": [
                drain_public.get(drain["id"])
                or _drain_public(conn, drain)
                for drain in history
            ],
            "unfinished_drain_count": sum(
                1 for drain in open_drains if drain["status"] == "incomplete"
            ),
            "active_drain_count": sum(
                1 for drain in open_drains if drain["status"] == "active"
            ),
            "limits": {"max_nodes": 4, "max_containers": 8},
        }


def _public_drain_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "node_id": row["node_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


# --------------------------------------------------------------------------- #
# Reconciliation (startup / after each command)
# --------------------------------------------------------------------------- #


def _finalize_drain(conn: sqlite3.Connection, drain_id: int) -> None:
    """Derive an open drain's status from the facts on the ground."""
    drain = conn.execute("SELECT * FROM drains WHERE id = ?", (drain_id,)).fetchone()
    if drain is None or drain["status"] not in OPEN_DRAIN_STATUSES:
        return
    node_id = drain["node_id"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM migrations WHERE drain_id = ? AND state = 'starting'",
        (drain_id,),
    ).fetchone()["n"]
    serving = conn.execute(
        "SELECT COUNT(*) AS n FROM containers WHERE node_id = ? AND status = 'serving'",
        (node_id,),
    ).fetchone()["n"]

    if pending:
        new_status = "active"
    elif serving:
        new_status = "incomplete"
    else:
        new_status = "completed"

    if new_status == drain["status"]:
        return
    if new_status == "completed":
        conn.execute(
            "UPDATE drains SET status = 'completed', finished_at = ? WHERE id = ?",
            (utcnow(), drain_id),
        )
        # Only report the node empty when nothing serves there and nothing is
        # reserved against it.
        reservations = conn.execute(
            "SELECT COUNT(*) AS n FROM migrations "
            "WHERE state = 'starting' AND target_node_id = ?",
            (node_id,),
        ).fetchone()["n"]
        if serving == 0 and reservations == 0:
            conn.execute(
                "UPDATE nodes SET status = 'drained' WHERE id = ?", (node_id,)
            )
    else:
        conn.execute(
            "UPDATE drains SET status = ?, finished_at = NULL WHERE id = ?",
            (new_status, drain_id),
        )
        conn.execute(
            "UPDATE nodes SET status = 'draining' WHERE id = ? AND status != 'drained'",
            (node_id,),
        )


def reconcile(db: Database) -> None:
    """Repair derived statuses after a restart or an interrupted request.

    Only touches bookkeeping flags; reservations and placements are untouched.
    """
    with db.write() as conn:
        for drain in conn.execute(
            "SELECT id FROM drains WHERE status IN ('active','incomplete')"
        ).fetchall():
            _finalize_drain(conn, drain["id"])
        busy_nodes = _draining_node_ids(conn)
        for node in _node_rows(conn):
            if node["id"] in busy_nodes and node["status"] == "drained":
                conn.execute(
                    "UPDATE nodes SET status = 'draining' WHERE id = ?", (node["id"],)
                )
            if node["id"] not in busy_nodes and node["status"] == "draining":
                serving = conn.execute(
                    "SELECT COUNT(*) AS n FROM containers "
                    "WHERE node_id = ? AND status = 'serving'",
                    (node["id"],),
                ).fetchone()["n"]
                status = "drained" if serving == 0 else "ready"
                conn.execute(
                    "UPDATE nodes SET status = ? WHERE id = ?", (status, node["id"])
                )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def import_topology(db: Database, topology: TopologyIn) -> dict[str, Any]:
    """Replace the entire simulation state atomically."""
    with db.write() as conn:
        # Children before parents (containers/drains reference nodes).
        conn.execute("DELETE FROM migrations")
        conn.execute("DELETE FROM drains")
        conn.execute("DELETE FROM containers")
        conn.execute("DELETE FROM services")
        conn.execute("DELETE FROM nodes")

        for position, node in enumerate(topology.nodes):
            conn.execute(
                "INSERT INTO nodes (id, name, cpu_capacity, status, position) "
                "VALUES (?, ?, ?, 'ready', ?)",
                (node.id, node.name or node.id, node.cpu_capacity, position),
            )

        seen_services: set[str] = set()
        for container in topology.containers:
            if container.service not in seen_services:
                seen_services.add(container.service)
                conn.execute(
                    "INSERT INTO services (name, min_ready, position) "
                    "VALUES (?, ?, ?)",
                    (
                        container.service,
                        container.min_ready,
                        len(seen_services) - 1,
                    ),
                )

        for position, container in enumerate(topology.containers):
            conn.execute(
                "INSERT INTO containers "
                "(id, name, service, cpu_request, node_id, status, position) "
                "VALUES (?, ?, ?, ?, ?, 'serving', ?)",
                (
                    container.id,
                    container.name or container.id,
                    container.service,
                    container.cpu_request,
                    container.node_id,
                    position,
                ),
            )
    return snapshot(db)


def reset(db: Database) -> dict[str, Any]:
    with db.write() as conn:
        conn.execute("DELETE FROM migrations")
        conn.execute("DELETE FROM drains")
        conn.execute("DELETE FROM containers")
        conn.execute("DELETE FROM services")
        conn.execute("DELETE FROM nodes")
    return snapshot(db)


def _record_blocked(
    conn: sqlite3.Connection,
    *,
    drain_id: int,
    container_id: str,
    source_node_id: str,
    cpu_request: int,
    code: str,
    message: str,
) -> None:
    now = utcnow()
    conn.execute(
        "INSERT INTO migrations "
        "(drain_id, container_id, source_node_id, target_node_id, state, "
        "reserved_cpu, error_code, error, created_at, updated_at) "
        "VALUES (?, ?, ?, NULL, 'blocked', ?, ?, ?, ?, ?)",
        (
            drain_id,
            container_id,
            source_node_id,
            cpu_request,
            code,
            message,
            now,
            now,
        ),
    )


def _candidate_targets(
    conn: sqlite3.Connection, source_node_id: str
) -> list[tuple[sqlite3.Row, int]]:
    """(node, free_cpu) for every node eligible to receive a new replica."""
    usage = _usage(conn)
    draining = _draining_node_ids(conn)
    candidates: list[tuple[sqlite3.Row, int]] = []
    for node in _node_rows(conn):
        if node["id"] == source_node_id:
            continue
        if node["id"] in draining or node["status"] != "ready":
            continue
        used = usage.get(node["id"], {"serving": 0, "reserved": 0})
        free = node["cpu_capacity"] - used["serving"] - used["reserved"]
        candidates.append((node, free))
    # Pack onto the emptiest node first: deterministic and leaves the most
    # room for the next replica.
    candidates.sort(key=lambda item: (-item[1], item[0]["position"], item[0]["id"]))
    return candidates


def _schedule(conn: sqlite3.Connection, drain_id: int, node_id: str) -> None:
    """Create one attempt per serving replica still on the drained node.

    Ordering uses each service's headroom above ``min_ready``: services with
    spare ready replicas are moved first, services that are exactly at their
    minimum are moved last. This never stops a replica before its replacement
    is ready, so the drain cannot pull a service below ``min_ready``; when
    capacity is scarce the ordering just decides who gets it first.
    """
    ready_by_service: dict[str, int] = {
        row["service"]: row["n"]
        for row in conn.execute(
            "SELECT service, COUNT(*) AS n FROM containers "
            "WHERE status = 'serving' GROUP BY service"
        ).fetchall()
    }
    min_ready = {row["name"]: row["min_ready"] for row in _service_rows(conn)}

    def surplus(container: sqlite3.Row) -> int:
        return ready_by_service.get(container["service"], 0) - min_ready.get(
            container["service"], 0
        )

    containers = sorted(
        _serving_containers(conn, node_id),
        key=lambda c: (-surplus(c), -c["cpu_request"], c["position"], c["id"]),
    )
    for container in containers:
        if conn.execute(
            "SELECT 1 FROM migrations WHERE container_id = ? AND state = 'starting'",
            (container["id"],),
        ).fetchone():
            # A replica is already starting for this container; its cutover
            # will free the source slot when resolved.
            continue

        candidates = _candidate_targets(conn, node_id)
        fitting = [
            (node, free)
            for node, free in candidates
            if free >= container["cpu_request"]
        ]
        if not fitting:
            if not candidates:
                code = "NO_USABLE_TARGET"
                message = (
                    "no other node can host a new replica (remaining nodes are "
                    "draining or drained)"
                )
            else:
                free_summary = ", ".join(
                    f"{node['id']}={free} CPU free" for node, free in candidates
                )
                code = "INSUFFICIENT_CAPACITY"
                message = (
                    f"no target node has {container['cpu_request']} CPU free "
                    f"({free_summary})"
                )
            _record_blocked(
                conn,
                drain_id=drain_id,
                container_id=container["id"],
                source_node_id=node_id,
                cpu_request=container["cpu_request"],
                code=code,
                message=message,
            )
            continue

        target = fitting[0][0]
        now = utcnow()
        conn.execute(
            "INSERT INTO migrations "
            "(drain_id, container_id, source_node_id, target_node_id, state, "
            "reserved_cpu, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'starting', ?, ?, ?)",
            (
                drain_id,
                container["id"],
                node_id,
                target["id"],
                container["cpu_request"],
                now,
                now,
            ),
        )


def start_drain(db: Database, node_id: str) -> dict[str, Any]:
    """Start a drain on a ready node, reserving capacity for every move."""
    with db.write() as conn:
        node = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if node is None:
            raise ApiError(404, "NODE_NOT_FOUND", f"node {node_id!r} does not exist")

        existing = _open_drain_for_node(conn, node_id)
        if existing is not None:
            code = (
                "DRAIN_ALREADY_ACTIVE"
                if existing["status"] == "active"
                else "DRAIN_INCOMPLETE"
            )
            raise ApiError(
                409,
                code,
                f"node {node_id!r} already has an open drain; resume it instead",
            )
        if node["status"] == "drained":
            raise ApiError(
                409, "NODE_ALREADY_DRAINED", f"node {node_id!r} is already drained"
            )
        inbound = _starting_migrations(conn, node_id)
        if inbound:
            raise ApiError(
                409,
                "INBOUND_RESERVATIONS_PENDING",
                "node has incoming replica reservations; resolve them before draining it",
            )

        conn.execute(
            "INSERT INTO drains (node_id, status, created_at) VALUES (?, 'active', ?)",
            (node_id, utcnow()),
        )
        drain_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute("UPDATE nodes SET status = 'draining' WHERE id = ?", (node_id,))

        _schedule(conn, drain_id, node_id)
        _finalize_drain(conn, drain_id)
    return snapshot(db)


def resume_drain(db: Database, node_id: str) -> dict[str, Any]:
    """Retry blocked replicas of an incomplete drain."""
    with db.write() as conn:
        node = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if node is None:
            raise ApiError(404, "NODE_NOT_FOUND", f"node {node_id!r} does not exist")
        drain = _open_drain_for_node(conn, node_id)
        if drain is None:
            raise ApiError(
                409, "NO_OPEN_DRAIN", f"node {node_id!r} has no open drain to resume"
            )
        if drain["status"] == "active":
            raise ApiError(
                409,
                "DRAIN_ALREADY_ACTIVE",
                "the drain is still active; resolve pending replicas first",
            )
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM migrations "
            "WHERE drain_id = ? AND state = 'starting'",
            (drain["id"],),
        ).fetchone()["n"]
        if pending:
            raise ApiError(
                409,
                "PENDING_REPLICAS",
                "new replicas are still starting; mark them ready or failed first",
            )

        conn.execute(
            "UPDATE drains SET status = 'active', finished_at = NULL WHERE id = ?",
            (drain["id"],),
        )
        _schedule(conn, drain["id"], node_id)
        _finalize_drain(conn, drain["id"])
    return snapshot(db)


def get_drain(db: Database, drain_id: int) -> dict[str, Any]:
    """Progress for one drain, including closed ones."""
    with db.read() as conn:
        drain = conn.execute("SELECT * FROM drains WHERE id = ?", (drain_id,)).fetchone()
        if drain is None:
            raise ApiError(404, "DRAIN_NOT_FOUND", f"drain {drain_id} does not exist")
        return _drain_public(conn, drain)


def restore_node(db: Database, node_id: str) -> dict[str, Any]:
    """Return a drained node to service so it can host replicas again."""
    with db.write() as conn:
        node = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if node is None:
            raise ApiError(404, "NODE_NOT_FOUND", f"node {node_id!r} does not exist")
        if node["status"] != "drained":
            raise ApiError(
                409,
                "NODE_NOT_DRAINED",
                f"node {node_id!r} is {node['status']!r}, not drained",
            )
        serving = conn.execute(
            "SELECT COUNT(*) AS n FROM containers "
            "WHERE node_id = ? AND status = 'serving'",
            (node_id,),
        ).fetchone()["n"]
        if serving:
            raise ApiError(
                409,
                "NODE_NOT_EMPTY",
                f"node {node_id!r} still has serving replicas",
            )
        conn.execute("UPDATE nodes SET status = 'ready' WHERE id = ?", (node_id,))
    return snapshot(db)


def resolve_migration(
    db: Database, migration_id: int, outcome: str, reason: str | None
) -> dict[str, Any]:
    """Simulate the new replica becoming ready (cutover) or failing to start."""
    with db.write() as conn:
        migration = conn.execute(
            "SELECT * FROM migrations WHERE id = ?", (migration_id,)
        ).fetchone()
        if migration is None:
            raise ApiError(
                404, "MIGRATION_NOT_FOUND", f"migration {migration_id} does not exist"
            )
        if migration["state"] != "starting":
            raise ApiError(
                409,
                "MIGRATION_NOT_PENDING",
                f"migration {migration_id} is {migration['state']!r}, not starting",
            )
        drain = conn.execute(
            "SELECT * FROM drains WHERE id = ?", (migration["drain_id"],)
        ).fetchone()
        if drain is None or drain["status"] not in OPEN_DRAIN_STATUSES:
            raise ApiError(
                409, "DRAIN_NOT_OPEN", "the drain for this replica is no longer open"
            )
        container = conn.execute(
            "SELECT * FROM containers WHERE id = ?", (migration["container_id"],)
        ).fetchone()
        if container is None or container["node_id"] != migration["source_node_id"]:
            raise ApiError(
                409,
                "SOURCE_REPLICA_GONE",
                "the old replica is no longer on the expected source node",
            )
        target_id = migration["target_node_id"]
        if outcome == "ready":
            target = conn.execute(
                "SELECT * FROM nodes WHERE id = ?", (target_id,)
            ).fetchone()
            usage = _usage(conn)
            target_usage = usage.get(target_id, {"serving": 0, "reserved": 0})
            others = target_usage["serving"] + target_usage["reserved"] - migration[
                "reserved_cpu"
            ]
            reservation_ok = (
                target is not None
                and target_id not in _draining_node_ids(conn)
                and target["cpu_capacity"] - others >= migration["reserved_cpu"]
            )
            if not reservation_ok:
                # Should be unreachable while reservations are honoured; keeps
                # the invariant explicit rather than silently overcommitting.
                conn.execute(
                    "UPDATE migrations SET state = 'failed', "
                    "error_code = 'RESERVATION_LOST', error = ?, updated_at = ? "
                    "WHERE id = ?",
                    (reason or "target reservation is no longer valid", utcnow(), migration_id),
                )
            else:
                # Atomic cutover: the new replica takes the reserved capacity
                # and the old replica stops in the same transaction.
                conn.execute(
                    "UPDATE containers SET node_id = ? WHERE id = ?",
                    (target_id, container["id"]),
                )
                conn.execute(
                    "UPDATE migrations SET state = 'completed', updated_at = ? "
                    "WHERE id = ?",
                    (utcnow(), migration_id),
                )
        else:
            # Failure releases the reservation; the old replica never stopped.
            conn.execute(
                "UPDATE migrations SET state = 'failed', "
                "error_code = 'REPLICA_START_FAILED', error = ?, updated_at = ? "
                "WHERE id = ?",
                (reason or "new replica failed to start", utcnow(), migration_id),
            )

        _finalize_drain(conn, migration["drain_id"])
    return snapshot(db)

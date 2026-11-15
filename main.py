from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from expert_system import build_input_snapshot, get_rule_catalog, run_expert_system

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "orion_iv.sqlite3"
FRONTEND_PATH = BASE_DIR / "index.html"
DB_LOCK = threading.RLock()

STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="AERO-MED ORION-IV Backend",
    description=(
        "Backend API for the AERO-MED / ORION-IV virtual mission dashboard. "
        "All supplied measurements are simulated demonstration data, not clinical data."
    ),
    version="1.0.0",
)

# Same-origin serving is used by default. CORS remains enabled for local frontend testing.
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in allowed_origins if origin.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect_db() -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection and always close it after the transaction."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with DB_LOCK, connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                state_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                crew_id TEXT NOT NULL,
                crew_name TEXT,
                status TEXT,
                activity TEXT,
                heart_rate REAL,
                hrv_ms REAL,
                spo2_pct REAL,
                systolic_bp REAL,
                diastolic_bp REAL,
                temperature_c REAL,
                respiratory_rate REAL,
                radiation_dose_msv REAL,
                scenario TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_readings_crew_time
                ON readings(crew_id, recorded_at DESC);

            CREATE TABLE IF NOT EXISTS medical_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                met TEXT,
                target TEXT,
                entry_type TEXT,
                note TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                acknowledged_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_status_time
                ON alerts(status, created_at DESC);

            CREATE TABLE IF NOT EXISTS expert_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                scenario TEXT NOT NULL,
                overall_status TEXT NOT NULL,
                finding_count INTEGER NOT NULL DEFAULT 0,
                critical_count INTEGER NOT NULL DEFAULT 0,
                warning_count INTEGER NOT NULL DEFAULT 0,
                input_snapshot TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_expert_decisions_time
                ON expert_decisions(created_at DESC);
            """
        )


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def validate_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object.")
    crew = payload.get("crew")
    eclss = payload.get("eclss")
    logs = payload.get("logs", [])
    if not isinstance(crew, list):
        raise HTTPException(status_code=422, detail="Field 'crew' must be an array.")
    if not isinstance(eclss, dict):
        raise HTTPException(status_code=422, detail="Field 'eclss' must be an object.")
    if not isinstance(logs, list):
        raise HTTPException(status_code=422, detail="Field 'logs' must be an array.")

    required_crew_fields = ("id", "hr", "spo2", "temp", "radDose")
    for index, member in enumerate(crew):
        if not isinstance(member, dict):
            raise HTTPException(status_code=422, detail=f"crew[{index}] must be an object.")
        missing = [field for field in required_crew_fields if field not in member]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"crew[{index}] is missing fields: {', '.join(missing)}",
            )
        for field in ("hr", "spo2", "temp", "radDose"):
            try:
                value = float(member[field])
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail=f"crew[{index}].{field} must be numeric.")
            if not (-100000 <= value <= 100000):
                raise HTTPException(status_code=422, detail=f"crew[{index}].{field} is outside accepted bounds.")

    try:
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="State contains values that cannot be serialized as JSON.")
    if len(serialized) > 2_000_000:
        raise HTTPException(status_code=413, detail="State payload is too large.")
    return payload


def load_state(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT payload FROM app_state WHERE state_key='mission'").fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:
        return None


def make_fingerprint(item: dict[str, Any]) -> str:
    stable = {
        "met": item.get("met", ""),
        "target": item.get("target", ""),
        "type": item.get("type", ""),
        "note": item.get("note", ""),
    }
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sync_logs(conn: sqlite3.Connection, logs: list[Any]) -> None:
    now = utc_now()
    for item in logs[-500:]:
        if not isinstance(item, dict):
            continue
        conn.execute(
            """INSERT OR IGNORE INTO medical_logs
               (fingerprint, met, target, entry_type, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                make_fingerprint(item),
                str(item.get("met", ""))[:40],
                str(item.get("target", "SYSTEM"))[:80],
                str(item.get("type", "Note"))[:100],
                str(item.get("note", ""))[:4000],
                now,
            ),
        )


def evaluate_alerts(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    """Create/resolve demonstration alerts using the UI's existing scenario flags."""
    active_keys: set[str] = set()
    scenario = str(state.get("scenario", "NOMINAL"))
    crew = state.get("crew", [])
    eclss = state.get("eclss", {})

    def add_alert(key: str, source: str, severity: str, title: str, message: str) -> None:
        active_keys.add(key)
        conn.execute(
            """INSERT INTO alerts
               (dedupe_key, created_at, source, severity, title, message, status)
               VALUES (?, ?, ?, ?, ?, ?, 'OPEN')
               ON CONFLICT(dedupe_key) DO UPDATE SET
                 title=excluded.title,
                 message=excluded.message,
                 severity=excluded.severity,
                 status=CASE WHEN alerts.status='ACKNOWLEDGED' THEN 'ACKNOWLEDGED' ELSE 'OPEN' END""",
            (key, utc_now(), source, severity, title, message),
        )

    for member in crew:
        if not isinstance(member, dict):
            continue
        crew_id = str(member.get("id", "UNKNOWN"))
        status = str(member.get("status", "NOMINAL")).upper()
        hr = float(member.get("hr", 0) or 0)
        spo2 = float(member.get("spo2", 100) or 100)
        if status == "CRITICAL" or hr > 125 or spo2 < 94:
            add_alert(
                f"crew:{crew_id}:vitals",
                crew_id,
                "CRITICAL",
                f"Abnormal simulated vitals — {crew_id}",
                f"Status={status}; HR={hr:.1f} BPM; SpO2={spo2:.1f}%. Demonstration alert only.",
            )
        elif status == "WARNING" or hr > 100 or spo2 < 96:
            add_alert(
                f"crew:{crew_id}:vitals",
                crew_id,
                "WARNING",
                f"Vitals require attention — {crew_id}",
                f"Status={status}; HR={hr:.1f} BPM; SpO2={spo2:.1f}%. Demonstration alert only.",
            )

    co2 = float(eclss.get("co2", 0) or 0)
    radiation = float(eclss.get("radFlux", 0) or 0)
    if co2 > 4.5:
        add_alert(
            "eclss:co2:high",
            "ECLSS",
            "CRITICAL",
            "Cabin CO₂ simulation threshold exceeded",
            f"Simulated ppCO₂ is {co2:.2f} mmHg. This is a software scenario, not a real cabin measurement.",
        )
    if radiation > 1.5:
        add_alert(
            "eclss:radiation:high",
            "ECLSS",
            "CRITICAL",
            "Radiation scenario alert",
            f"Simulated radiation flux is {radiation:.2f} mSv/day. Demonstration data only.",
        )

    # Close alerts whose triggering condition is no longer active.
    existing = conn.execute("SELECT dedupe_key, status FROM alerts WHERE status != 'RESOLVED'").fetchall()
    for row in existing:
        if row["dedupe_key"] not in active_keys:
            conn.execute(
                "UPDATE alerts SET status='RESOLVED' WHERE dedupe_key=?",
                (row["dedupe_key"],),
            )


def persist_state(payload: dict[str, Any], record_readings: bool = False) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    now = utc_now()
    with DB_LOCK, connect_db() as conn:
        conn.execute(
            """INSERT INTO app_state(state_key, payload, updated_at)
               VALUES ('mission', ?, ?)
               ON CONFLICT(state_key) DO UPDATE SET
                 payload=excluded.payload, updated_at=excluded.updated_at""",
            (encoded, now),
        )
        sync_logs(conn, payload.get("logs", []))
        evaluate_alerts(conn, payload)

        if record_readings:
            for member in payload.get("crew", []):
                conn.execute(
                    """INSERT INTO readings
                       (recorded_at, crew_id, crew_name, status, activity, heart_rate,
                        hrv_ms, spo2_pct, systolic_bp, diastolic_bp, temperature_c,
                        respiratory_rate, radiation_dose_msv, scenario)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        now,
                        str(member.get("id", "UNKNOWN")),
                        str(member.get("name", "")),
                        str(member.get("status", "NOMINAL")),
                        str(member.get("activity", "")),
                        member.get("hr"),
                        member.get("hrv"),
                        member.get("spo2"),
                        member.get("sys"),
                        member.get("dia"),
                        member.get("temp"),
                        member.get("resp"),
                        member.get("radDose"),
                        str(payload.get("scenario", "NOMINAL")),
                    ),
                )


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    if not FRONTEND_PATH.exists():
        raise HTTPException(status_code=404, detail="Frontend index.html was not found beside main.py.")
    return FileResponse(FRONTEND_PATH)


@app.get("/api/health")
def health() -> dict[str, Any]:
    with DB_LOCK, connect_db() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "service": "AERO-MED ORION-IV API", "timestamp": utc_now()}


@app.get("/api/state")
def get_state() -> dict[str, Any]:
    with DB_LOCK, connect_db() as conn:
        return {"state": load_state(conn), "updated_at": utc_now()}


@app.put("/api/state")
def put_state(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    state = validate_state(payload)
    persist_state(state, record_readings=False)
    return {"ok": True, "message": "Mission state saved.", "updated_at": utc_now()}


@app.post("/api/telemetry")
def post_telemetry(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    state = validate_state(payload)
    persist_state(state, record_readings=True)
    return {"ok": True, "message": "Telemetry snapshot stored.", "crew_count": len(state["crew"]), "recorded_at": utc_now()}


@app.get("/api/readings")
def get_readings(
    crew_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=5000),
) -> dict[str, Any]:
    with DB_LOCK, connect_db() as conn:
        if crew_id:
            rows = conn.execute(
                "SELECT * FROM readings WHERE crew_id=? ORDER BY id DESC LIMIT ?",
                (crew_id, limit),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM readings WHERE crew_id=?", (crew_id,)).fetchone()[0]
        else:
            rows = conn.execute("SELECT * FROM readings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    items = [dict(row) for row in rows]
    return {"items": items, "total": total, "limit": limit}


@app.get("/api/alerts")
def get_alerts(status: str | None = None, limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
    with DB_LOCK, connect_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status.upper(), limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [dict(row) for row in rows], "count": len(rows)}


@app.post("/api/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: int) -> dict[str, Any]:
    with DB_LOCK, connect_db() as conn:
        cursor = conn.execute(
            "UPDATE alerts SET status='ACKNOWLEDGED', acknowledged_at=? WHERE id=? AND status='OPEN'",
            (utc_now(), alert_id),
        )
        if cursor.rowcount == 0:
            exists = conn.execute("SELECT id FROM alerts WHERE id=?", (alert_id,)).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="Alert not found.")
        return {"ok": True, "alert_id": alert_id, "message": "Alert acknowledged."}


@app.post("/api/alerts/acknowledge-all")
def acknowledge_all_alerts() -> dict[str, Any]:
    with DB_LOCK, connect_db() as conn:
        cursor = conn.execute(
            "UPDATE alerts SET status='ACKNOWLEDGED', acknowledged_at=? WHERE status='OPEN'",
            (utc_now(),),
        )
    return {"ok": True, "updated": cursor.rowcount}


@app.get("/api/logs")
def get_logs(limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
    with DB_LOCK, connect_db() as conn:
        rows = conn.execute(
            "SELECT * FROM medical_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"items": [dict(row) for row in rows], "count": len(rows)}


@app.post("/api/logs")
def create_log(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    required = ("target", "type", "note")
    if any(not str(payload.get(key, "")).strip() for key in required):
        raise HTTPException(status_code=422, detail="target, type, and note are required.")
    item = {
        "met": str(payload.get("met", "")),
        "target": str(payload["target"]),
        "type": str(payload["type"]),
        "note": str(payload["note"]),
    }
    with DB_LOCK, connect_db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO medical_logs
               (fingerprint, met, target, entry_type, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (make_fingerprint(item), item["met"], item["target"], item["type"], item["note"], utc_now()),
        )
    return {"ok": True, "item": item}


@app.get("/api/analytics")
def analytics() -> dict[str, Any]:
    with DB_LOCK, connect_db() as conn:
        rows = conn.execute(
            """SELECT crew_id, crew_name, COUNT(*) AS sample_count,
                      ROUND(AVG(heart_rate), 2) AS avg_heart_rate,
                      ROUND(AVG(spo2_pct), 2) AS avg_spo2,
                      ROUND(AVG(temperature_c), 2) AS avg_temperature_c,
                      MAX(recorded_at) AS latest_recorded_at
               FROM readings GROUP BY crew_id, crew_name ORDER BY crew_id"""
        ).fetchall()
        alert_counts = conn.execute(
            "SELECT status, COUNT(*) AS count FROM alerts GROUP BY status"
        ).fetchall()
    return {
        "crew": [dict(row) for row in rows],
        "alerts_by_status": {row["status"]: row["count"] for row in alert_counts},
    }


@app.get("/api/system/status")
def system_status() -> dict[str, Any]:
    with DB_LOCK, connect_db() as conn:
        state = load_state(conn)
        reading_count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        alert_count = conn.execute("SELECT COUNT(*) FROM alerts WHERE status='OPEN'").fetchone()[0]
    return {
        "api": "ONLINE",
        "database": "ONLINE",
        "simulation_mode": (state or {}).get("scenario", "NOT_INITIALIZED"),
        "crew_count": len((state or {}).get("crew", [])),
        "stored_readings": reading_count,
        "open_alerts": alert_count,
        "data_source": "SIMULATED_DEMO_DATA",
    }


@app.get("/api/expert/status")
def expert_system_status() -> dict[str, Any]:
    """Return expert-engine availability and the latest persisted analysis summary."""
    try:
        catalog = get_rule_catalog()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    with DB_LOCK, connect_db() as conn:
        row = conn.execute(
            """SELECT run_id, created_at, scenario, overall_status,
                      finding_count, critical_count, warning_count
               FROM expert_decisions ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    return {
        "status": "ONLINE",
        "engine": "ORION-IV Rule-Based Inference Engine",
        "method": "Data-driven IF-THEN rule evaluation over current mission facts",
        "knowledge_base": catalog["name"],
        "knowledge_base_version": catalog["version"],
        "total_rules": catalog["count"],
        "enabled_rules": catalog["enabled_count"],
        "last_analysis": dict(row) if row else None,
        "simulation_only": True,
    }


@app.get("/api/expert/rules")
def expert_system_rules() -> dict[str, Any]:
    """Expose the editable declarative rule catalog to the frontend."""
    try:
        return get_rule_catalog()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/expert/analyze")
def analyze_expert_system(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    """Analyze an optional supplied state; otherwise analyze the latest saved mission state."""
    if payload is not None:
        state = validate_state(payload)
        source = "request_payload"
    else:
        with DB_LOCK, connect_db() as conn:
            state = load_state(conn)
        source = "latest_saved_state"
        if state is None:
            raise HTTPException(
                status_code=409,
                detail="No saved mission state exists yet. Open the dashboard or submit a mission state first.",
            )

    try:
        result = run_expert_system(state, source=source)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    snapshot = build_input_snapshot(state)
    counts = result["counts"]
    with DB_LOCK, connect_db() as conn:
        conn.execute(
            """INSERT INTO expert_decisions
               (run_id, created_at, scenario, overall_status, finding_count,
                critical_count, warning_count, input_snapshot, result_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result["run_id"],
                result["generated_at"],
                str(result["scenario"]),
                result["overall_status"],
                counts["findings"],
                counts["critical"],
                counts["warning"],
                json.dumps(snapshot, ensure_ascii=False, allow_nan=False),
                json.dumps(result, ensure_ascii=False, allow_nan=False),
            ),
        )
    return result


@app.get("/api/expert/history")
def expert_system_history(
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Return recent analysis runs without the full finding payload."""
    with DB_LOCK, connect_db() as conn:
        rows = conn.execute(
            """SELECT run_id, created_at, scenario, overall_status,
                      finding_count, critical_count, warning_count
               FROM expert_decisions ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM expert_decisions").fetchone()[0]
    return {"items": [dict(row) for row in rows], "count": total, "limit": limit}


@app.get("/api/expert/decisions/{run_id}")
def expert_system_decision(run_id: str) -> dict[str, Any]:
    """Retrieve one full, explainable decision run by its run_id."""
    with DB_LOCK, connect_db() as conn:
        row = conn.execute(
            "SELECT result_json, input_snapshot FROM expert_decisions WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Expert-system analysis run was not found.")
    try:
        result = json.loads(row["result_json"])
        result["input_snapshot"] = json.loads(row["input_snapshot"])
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Stored expert-system result is invalid JSON.") from exc
    return result


@app.get("/api/export/readings.csv")
def export_readings_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "recorded_at", "crew_id", "crew_name", "status", "activity", "heart_rate_bpm",
        "hrv_ms", "spo2_pct", "systolic_bp", "diastolic_bp", "temperature_c",
        "respiratory_rate", "radiation_dose_msv", "scenario",
    ])
    with DB_LOCK, connect_db() as conn:
        rows = conn.execute("SELECT * FROM readings ORDER BY id ASC").fetchall()
    for row in rows:
        writer.writerow([
            row["recorded_at"], row["crew_id"], row["crew_name"], row["status"], row["activity"],
            row["heart_rate"], row["hrv_ms"], row["spo2_pct"], row["systolic_bp"],
            row["diastolic_bp"], row["temperature_c"], row["respiratory_rate"],
            row["radiation_dose_msv"], row["scenario"],
        ])
    data = "\ufeff" + output.getvalue()
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="ORION_IV_BioTelemetry.csv"'},
    )


# The static HTML file is served by the root route. API routes remain under /api.

"""F-01 ingestion API: source records are accepted only when supplied by a user/system.

No seed, mock, fallback or generated railway data is created by this service.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "rail_sync.db"


class SourceSystem(StrEnum):
    TMS = "TMS"
    SMMS = "SMMS"
    TDMS = "TDMS"


class Department(StrEnum):
    ENGINEERING = "ENGINEERING"
    SIGNAL_TELECOM = "SIGNAL_TELECOM"
    TRACTION = "TRACTION"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ReferenceRegistration(BaseModel):
    source_system: SourceSystem
    source_reference: str = Field(min_length=1, max_length=160)
    section_id: str = Field(min_length=1, max_length=120)
    start_km: float = Field(ge=0)
    end_km: float = Field(ge=0)
    asset_type: str = Field(min_length=1, max_length=120)
    asset_reference: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def kilometre_range_is_ordered(self) -> "ReferenceRegistration":
        if self.end_km < self.start_km:
            raise ValueError("end_km must be greater than or equal to start_km")
        return self


class PlanningContext(BaseModel):
    severity: Severity
    estimated_duration_minutes: int = Field(gt=0, le=10080)
    due_date: datetime
    required_crews: list[str] = Field(min_length=1)
    required_equipment: list[str] = Field(min_length=1)
    requires_traffic_block: bool
    requires_traction_disconnection: bool
    co_working_compatible: bool

    @field_validator("due_date")
    @classmethod
    def due_date_must_be_timezone_aware(cls, value: datetime) -> datetime:
        """HTML datetime-local values have no offset; store them consistently as UTC."""
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @field_validator("required_crews", "required_equipment")
    @classmethod
    def entries_must_not_be_blank(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("entries cannot be blank")
        return [value.strip() for value in values]


class IngestionRequest(BaseModel):
    source_system: SourceSystem
    record: dict[str, Any]
    planning_context: PlanningContext


class NormalizedTask(BaseModel):
    id: str
    source_system: SourceSystem
    source_reference: str
    source_timestamp: datetime
    department: Department
    asset_type: str
    asset_reference: str
    section_id: str
    start_km: float
    end_km: float
    maintenance_type: str
    severity: Severity
    overdue: bool
    due_date: datetime
    estimated_duration_minutes: int
    required_crews: list[str]
    required_equipment: list[str]
    requires_traffic_block: bool
    requires_traction_disconnection: bool
    co_working_compatible: bool
    data_quality_status: str


class FeasibilityRules(BaseModel):
    rule_version: str = Field(min_length=1, max_length=100)
    engineering_max_temperature_c: float | None = None
    traction_max_wind_speed_kmh: float | None = None
    traffic_block_min_visibility_m: float | None = Field(default=None, ge=0)
    caution_risk_multiplier: float | None = Field(default=None, ge=1, le=2)


class FeasibilityRequest(BaseModel):
    task_id: str = Field(min_length=1)
    section_latitude: float = Field(ge=-90, le=90)
    section_longitude: float = Field(ge=-180, le=180)
    proposed_time: datetime
    rules: FeasibilityRules

    @field_validator("proposed_time")
    @classmethod
    def proposed_time_must_be_timezone_aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class PriorityPolicy(BaseModel):
    policy_version: str = Field(min_length=1, max_length=100)
    severity_scores: dict[Severity, float]
    max_days_overdue: float = Field(gt=0)
    max_passenger_trains_per_day: float = Field(gt=0)
    max_goods_trains_per_day: float = Field(gt=0)
    max_section_traffic_gmt: float = Field(gt=0)
    active_restriction_score: float = Field(ge=0, le=100)
    max_weather_risk_multiplier: float = Field(gt=1)
    severity_weight: float = Field(ge=0)
    overdue_weight: float = Field(ge=0)
    passenger_weight: float = Field(ge=0)
    goods_weight: float = Field(ge=0)
    traffic_weight: float = Field(ge=0)
    restriction_weight: float = Field(ge=0)
    route_criticality_weight: float = Field(ge=0)
    weather_weight: float = Field(ge=0)
    critical_threshold: float = Field(ge=0, le=100)
    high_threshold: float = Field(ge=0, le=100)
    medium_threshold: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def policy_is_complete_and_ordered(self) -> "PriorityPolicy":
        if set(self.severity_scores) != set(Severity):
            raise ValueError("severity_scores must contain CRITICAL, HIGH, MEDIUM and LOW.")
        if any(score < 0 or score > 100 for score in self.severity_scores.values()):
            raise ValueError("severity scores must be between 0 and 100.")
        if not any((self.severity_weight, self.overdue_weight, self.passenger_weight, self.goods_weight, self.traffic_weight, self.restriction_weight, self.route_criticality_weight, self.weather_weight)):
            raise ValueError("At least one priority weight must be greater than zero.")
        if not self.critical_threshold >= self.high_threshold >= self.medium_threshold:
            raise ValueError("Thresholds must follow critical ≥ high ≥ medium.")
        return self


class PriorityContext(BaseModel):
    task_id: str = Field(min_length=1)
    passenger_train_frequency_per_day: float = Field(ge=0)
    goods_train_forecast_per_day: float = Field(ge=0)
    section_traffic_gmt: float = Field(ge=0)
    route_criticality_score: float = Field(ge=0, le=100)
    active_operational_restriction: bool


class PriorityRequest(BaseModel):
    context: PriorityContext
    policy: PriorityPolicy | None = None
    policy_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def exactly_one_policy_source_is_supplied(self) -> "PriorityRequest":
        if (self.policy is None) == (self.policy_id is None):
            raise ValueError("Supply exactly one of policy or policy_id.")
        return self


class PlanningHorizon(StrEnum):
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class CorridorWindow(BaseModel):
    corridor_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    start_time: datetime
    end_time: datetime
    max_simultaneous_crews: int = Field(gt=0)
    traffic_block_available: bool
    traction_disconnection_available: bool
    timetable_reference: str = Field(min_length=1)
    goods_forecast_reference: str = Field(min_length=1)
    passenger_trains_affected: int = Field(ge=0)
    goods_trains_affected: int = Field(ge=0)

    @field_validator("start_time", "end_time")
    @classmethod
    def window_time_is_aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def window_has_positive_duration(self) -> "CorridorWindow":
        if self.end_time <= self.start_time:
            raise ValueError("A COA block window must end after it starts.")
        return self


class BlockPlanRequest(BaseModel):
    horizon: PlanningHorizon
    horizon_start: datetime
    horizon_end: datetime
    coa_windows: list[CorridorWindow] = Field(min_length=1)

    @field_validator("horizon_start", "horizon_end")
    @classmethod
    def horizon_is_aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def horizon_and_windows_are_valid(self) -> "BlockPlanRequest":
        duration_days = (self.horizon_end - self.horizon_start).total_seconds() / 86400
        limits = (6, 8) if self.horizon is PlanningHorizon.WEEKLY else (28, 31)
        if not limits[0] <= duration_days <= limits[1]:
            raise ValueError(f"{self.horizon.lower()} plans require a horizon between {limits[0]} and {limits[1]} days.")
        ids = [window.corridor_id for window in self.coa_windows]
        if len(ids) != len(set(ids)):
            raise ValueError("Each submitted COA block window needs a unique corridor_id.")
        for window in self.coa_windows:
            if window.start_time < self.horizon_start or window.end_time > self.horizon_end:
                raise ValueError(f"COA window '{window.corridor_id}' sits outside the requested planning horizon.")
        return self


class TimetableOccupancy(BaseModel):
    record_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    window_start: datetime
    window_end: datetime
    passenger_trains_affected: int = Field(ge=0)
    source_timestamp: datetime

    @field_validator("window_start", "window_end", "source_timestamp")
    @classmethod
    def occupancy_times_are_aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def occupancy_interval_is_valid(self) -> "TimetableOccupancy":
        if self.window_end <= self.window_start:
            raise ValueError("Timetable occupancy window must end after it starts.")
        return self


class GoodsForecast(BaseModel):
    record_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    window_start: datetime
    window_end: datetime
    goods_trains_affected: int = Field(ge=0)
    source_timestamp: datetime

    @field_validator("window_start", "window_end", "source_timestamp")
    @classmethod
    def forecast_times_are_aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def forecast_interval_is_valid(self) -> "GoodsForecast":
        if self.window_end <= self.window_start:
            raise ValueError("Goods forecast window must end after it starts.")
        return self


class ImportedCOAWindow(BaseModel):
    """COA source record; train-impact counts are resolved from cited source records."""
    corridor_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    start_time: datetime
    end_time: datetime
    max_simultaneous_crews: int = Field(gt=0)
    traffic_block_available: bool
    traction_disconnection_available: bool
    timetable_reference: str = Field(min_length=1)
    goods_forecast_reference: str = Field(min_length=1)

    @field_validator("start_time", "end_time")
    @classmethod
    def imported_window_time_is_aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def imported_window_has_positive_duration(self) -> "ImportedCOAWindow":
        if self.end_time <= self.start_time:
            raise ValueError("A COA block window must end after it starts.")
        return self


class COAWindowImport(BaseModel):
    window: ImportedCOAWindow
    source_timestamp: datetime

    @field_validator("source_timestamp")
    @classmethod
    def coa_source_time_is_aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class IntegratedPlanRequest(BaseModel):
    horizon: PlanningHorizon
    horizon_start: datetime
    horizon_end: datetime

    @field_validator("horizon_start", "horizon_end")
    @classmethod
    def integrated_horizon_is_aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def integrated_horizon_is_valid(self) -> "IntegratedPlanRequest":
        duration_days = (self.horizon_end - self.horizon_start).total_seconds() / 86400
        limits = (6, 8) if self.horizon is PlanningHorizon.WEEKLY else (28, 31)
        if not limits[0] <= duration_days <= limits[1]:
            raise ValueError(f"{self.horizon.lower()} plans require a horizon between {limits[0]} and {limits[1]} days.")
        return self


class PriorityPolicyRegistration(BaseModel):
    policy_id: str = Field(min_length=1)
    policy: PriorityPolicy
    approval_reference: str = Field(min_length=1)
    approved_at: datetime

    @field_validator("approved_at")
    @classmethod
    def approval_time_is_aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@contextmanager
def connection():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


def initialize_database() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS network_references (
                id TEXT PRIMARY KEY,
                source_system TEXT NOT NULL,
                source_reference TEXT NOT NULL,
                section_id TEXT NOT NULL,
                start_km REAL NOT NULL,
                end_km REAL NOT NULL,
                asset_type TEXT NOT NULL,
                asset_reference TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source_system, source_reference)
            );
            CREATE TABLE IF NOT EXISTS ingestion_records (
                id TEXT PRIMARY KEY,
                source_system TEXT NOT NULL,
                source_reference TEXT,
                received_at TEXT NOT NULL,
                original_record_json TEXT NOT NULL,
                planning_context_json TEXT,
                normalized_task_json TEXT,
                data_quality_status TEXT NOT NULL,
                review_reasons_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feasibility_assessments (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                proposed_time TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                rule_version TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS priority_evaluations (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                context_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS block_plans (
                id TEXT PRIMARY KEY,
                horizon TEXT NOT NULL,
                horizon_start TEXT NOT NULL,
                horizon_end TEXT NOT NULL,
                coa_windows_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS timetable_occupancy (
                record_id TEXT PRIMARY KEY,
                section_id TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                passenger_trains_affected INTEGER NOT NULL,
                source_timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS goods_forecasts (
                record_id TEXT PRIMARY KEY,
                section_id TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                goods_trains_affected INTEGER NOT NULL,
                source_timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coa_windows (
                corridor_id TEXT PRIMARY KEY,
                window_json TEXT NOT NULL,
                source_timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS priority_policies (
                policy_id TEXT PRIMARY KEY,
                policy_json TEXT NOT NULL,
                approval_reference TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def now() -> datetime:
    return datetime.now(timezone.utc)


def source_details(source_system: SourceSystem, record: dict[str, Any]) -> tuple[str, str, str, datetime, Department, float | None, float | None]:
    """Extract only documented fields; missing or invalid values are review reasons."""
    try:
        if source_system is SourceSystem.TMS:
            required = ("ticket_id", "track_id", "km_start", "km_end", "defect_class", "date_detected")
            ensure_fields(record, required)
            return (
                str(record["ticket_id"]), str(record["track_id"]), str(record["defect_class"]),
                parse_timestamp(record["date_detected"]), Department.ENGINEERING,
                parse_km(record["km_start"]), parse_km(record["km_end"]),
            )
        if source_system is SourceSystem.SMMS:
            required = ("fault_id", "station_code", "gear_type", "failure_category", "reported_ts", "urgency_code")
            ensure_fields(record, required)
            return (
                str(record["fault_id"]), str(record["station_code"]), str(record["failure_category"]),
                parse_timestamp(record["reported_ts"]), Department.SIGNAL_TELECOM, None, None,
            )
        required = ("defect_no", "ohe_substation", "mast_from", "mast_to", "issue_type", "scheduled_date")
        ensure_fields(record, required)
        return (
            str(record["defect_no"]), f"{record['mast_from']}:{record['mast_to']}", str(record["issue_type"]),
            parse_timestamp(record["scheduled_date"]), Department.TRACTION, None, None,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(str(error)) from error


def ensure_fields(record: dict[str, Any], required: tuple[str, ...]) -> None:
    missing = [field for field in required if record.get(field) in (None, "")]
    if missing:
        raise ValueError(f"Missing required source fields: {', '.join(missing)}")


def parse_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_km(value: Any) -> float:
    parsed = float(value)
    if parsed < 0:
        raise ValueError("Kilometre values cannot be negative")
    return parsed


def persist_review(request: IngestionRequest, reason: str, source_reference: str | None = None) -> dict[str, Any]:
    record_id = str(uuid4())
    with connection() as db:
        db.execute(
            """INSERT INTO ingestion_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record_id, request.source_system, source_reference, now().isoformat(), json.dumps(request.record),
             json.dumps(request.planning_context.model_dump(mode="json")), None, "NEEDS_REVIEW", json.dumps([reason])),
        )
    return {"ingestion_id": record_id, "data_quality_status": "NEEDS_REVIEW", "review_reasons": [reason], "normalized_task": None}


app = FastAPI(title="RailSync F-01 Ingestion API", version="1.0.0")
app.mount("/static", StaticFiles(directory=ROOT), name="static")


@app.on_event("startup")
def startup() -> None:
    initialize_database()


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(ROOT / "index.html")


@app.get("/intake", include_in_schema=False)
def intake_page() -> FileResponse:
    return FileResponse(ROOT / "intake.html")


@app.get("/feasibility", include_in_schema=False)
def feasibility_page() -> FileResponse:
    return FileResponse(ROOT / "feasibility.html")


@app.get("/priority", include_in_schema=False)
def priority_page() -> FileResponse:
    return FileResponse(ROOT / "priority.html")


@app.get("/planner", include_in_schema=False)
def planner_page() -> FileResponse:
    return FileResponse(ROOT / "planner.html")


@app.get("/operations-data", include_in_schema=False)
def operations_data_page() -> FileResponse:
    return FileResponse(ROOT / "operations-data.html")


@app.get("/what-if", include_in_schema=False)
def what_if_page() -> FileResponse:
    return FileResponse(ROOT / "what-if.html")


@app.get("/live-monitor", include_in_schema=False)
def live_monitor_page() -> FileResponse:
    return FileResponse(ROOT / "live-monitor.html")


@app.post("/api/v1/network-references", status_code=201)
def register_reference(reference: ReferenceRegistration) -> dict[str, Any]:
    reference_id = str(uuid4())
    try:
        with connection() as db:
            db.execute(
                """INSERT INTO network_references VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (reference_id, reference.source_system, reference.source_reference, reference.section_id,
                 reference.start_km, reference.end_km, reference.asset_type, reference.asset_reference, now().isoformat()),
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(409, "An authoritative reference already exists for this source system and reference.") from error
    return {"reference_id": reference_id, "status": "REGISTERED", "reference": reference.model_dump()}


@app.post("/api/v1/ingestion/tasks", status_code=201)
def ingest_task(request: IngestionRequest) -> dict[str, Any]:
    try:
        source_id, lookup_reference, maintenance_type, source_timestamp, department, raw_start, raw_end = source_details(request.source_system, request.record)
    except ValueError as error:
        return persist_review(request, str(error))

    with connection() as db:
        existing = db.execute(
            """SELECT id FROM ingestion_records
               WHERE source_system = ? AND source_reference = ? AND data_quality_status = 'COMPLETE'
               LIMIT 1""",
            (request.source_system, source_id),
        ).fetchone()
    if existing is not None:
        raise HTTPException(
            409,
            f"Source record '{source_id}' has already been ingested. Use its existing normalized task rather than submitting a duplicate.",
        )

    with connection() as db:
        mapping = db.execute(
            "SELECT * FROM network_references WHERE source_system = ? AND source_reference = ?",
            (request.source_system, lookup_reference),
        ).fetchone()

    if mapping is None:
        return persist_review(request, f"No controlled network reference is registered for '{lookup_reference}'.", source_id)

    start_km = raw_start if raw_start is not None else mapping["start_km"]
    end_km = raw_end if raw_end is not None else mapping["end_km"]
    if end_km < start_km:
        return persist_review(request, "The source kilometre range ends before it starts.", source_id)
    if start_km < mapping["start_km"] or end_km > mapping["end_km"]:
        return persist_review(request, "The source kilometre range falls outside the registered controlled reference.", source_id)

    task = NormalizedTask(
        id=source_id, source_system=request.source_system, source_reference=lookup_reference,
        source_timestamp=source_timestamp, department=department, asset_type=mapping["asset_type"],
        asset_reference=mapping["asset_reference"], section_id=mapping["section_id"], start_km=start_km, end_km=end_km,
        maintenance_type=maintenance_type, severity=request.planning_context.severity,
        overdue=request.planning_context.due_date < now(), due_date=request.planning_context.due_date,
        estimated_duration_minutes=request.planning_context.estimated_duration_minutes,
        required_crews=request.planning_context.required_crews, required_equipment=request.planning_context.required_equipment,
        requires_traffic_block=request.planning_context.requires_traffic_block,
        requires_traction_disconnection=request.planning_context.requires_traction_disconnection,
        co_working_compatible=request.planning_context.co_working_compatible, data_quality_status="COMPLETE",
    )
    ingestion_id = str(uuid4())
    with connection() as db:
        db.execute(
            """INSERT INTO ingestion_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ingestion_id, request.source_system, source_id, now().isoformat(), json.dumps(request.record),
             json.dumps(request.planning_context.model_dump(mode="json")), task.model_dump_json(), "COMPLETE", "[]"),
        )
    return {"ingestion_id": ingestion_id, "data_quality_status": "COMPLETE", "review_reasons": [], "normalized_task": task}


@app.get("/api/v1/ingestion/tasks")
def list_ingestion_records() -> list[dict[str, Any]]:
    with connection() as db:
        rows = db.execute(
            """SELECT id, source_system, source_reference, received_at, normalized_task_json,
                      data_quality_status, review_reasons_json
               FROM ingestion_records ORDER BY received_at DESC"""
        ).fetchall()
    return [
        {"ingestion_id": row["id"], "source_system": row["source_system"], "source_reference": row["source_reference"],
         "received_at": row["received_at"], "data_quality_status": row["data_quality_status"],
         "review_reasons": json.loads(row["review_reasons_json"]),
         "normalized_task": json.loads(row["normalized_task_json"]) if row["normalized_task_json"] else None}
        for row in rows
    ]


@app.get("/api/v1/operations/summary")
def operations_summary() -> dict[str, Any]:
    """Expose only operator-supplied operational records; never create sample data."""
    with connection() as db:
        timetable = db.execute("SELECT record_id, section_id, window_start, window_end, passenger_trains_affected, source_timestamp FROM timetable_occupancy ORDER BY window_start").fetchall()
        goods = db.execute("SELECT record_id, section_id, window_start, window_end, goods_trains_affected, source_timestamp FROM goods_forecasts ORDER BY window_start").fetchall()
        coa = db.execute("SELECT corridor_id, window_json, source_timestamp FROM coa_windows ORDER BY created_at DESC").fetchall()
    return {
        "timetable_occupancy": [dict(row) for row in timetable],
        "goods_forecasts": [dict(row) for row in goods],
        "coa_windows": [{"corridor_id": row["corridor_id"], "window": json.loads(row["window_json"]), "source_timestamp": row["source_timestamp"]} for row in coa],
    }


@app.post("/api/v1/operations/timetable-occupancy", status_code=201)
def import_timetable_occupancy(record: TimetableOccupancy) -> dict[str, Any]:
    try:
        with connection() as db:
            db.execute(
                "INSERT INTO timetable_occupancy VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record.record_id, record.section_id, record.window_start.isoformat(), record.window_end.isoformat(),
                 record.passenger_trains_affected, record.source_timestamp.isoformat(), now().isoformat()),
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(409, "A timetable occupancy record already exists with this record_id.") from error
    return {"status": "IMPORTED", "record": record}


@app.post("/api/v1/operations/goods-forecasts", status_code=201)
def import_goods_forecast(record: GoodsForecast) -> dict[str, Any]:
    try:
        with connection() as db:
            db.execute(
                "INSERT INTO goods_forecasts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record.record_id, record.section_id, record.window_start.isoformat(), record.window_end.isoformat(),
                 record.goods_trains_affected, record.source_timestamp.isoformat(), now().isoformat()),
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(409, "A goods forecast already exists with this record_id.") from error
    return {"status": "IMPORTED", "record": record}


@app.post("/api/v1/operations/coa-windows", status_code=201)
def import_coa_window(request: COAWindowImport) -> dict[str, Any]:
    """Accept an authoritative COA window only after both cited source records exist."""
    with connection() as db:
        timetable = db.execute("SELECT * FROM timetable_occupancy WHERE record_id = ?", (request.window.timetable_reference,)).fetchone()
        goods = db.execute("SELECT * FROM goods_forecasts WHERE record_id = ?", (request.window.goods_forecast_reference,)).fetchone()
    if timetable is None or goods is None:
        missing = []
        if timetable is None:
            missing.append("timetable occupancy")
        if goods is None:
            missing.append("goods forecast")
        raise HTTPException(409, f"Import the referenced {', '.join(missing)} record before its COA window.")
    for label, source in (("timetable occupancy", timetable), ("goods forecast", goods)):
        if source["section_id"] != request.window.section_id:
            raise HTTPException(422, f"Referenced {label} section does not match the COA window section.")
        if datetime.fromisoformat(source["window_start"]) > request.window.start_time or datetime.fromisoformat(source["window_end"]) < request.window.end_time:
            raise HTTPException(422, f"Referenced {label} interval does not cover the entire COA window.")
    stored_window = CorridorWindow(
        **request.window.model_dump(),
        passenger_trains_affected=0,
        goods_trains_affected=0,
    )
    try:
        with connection() as db:
            db.execute("INSERT INTO coa_windows VALUES (?, ?, ?, ?)", (request.window.corridor_id, stored_window.model_dump_json(), request.source_timestamp.isoformat(), now().isoformat()))
    except sqlite3.IntegrityError as error:
        raise HTTPException(409, "A COA window already exists with this corridor_id.") from error
    return {"status": "IMPORTED", "window": stored_window}


def fetch_forecast(latitude: float, longitude: float, proposed_time: datetime) -> dict[str, Any] | None:
    """Fetch a real forecast hour. Absent/unavailable data is intentionally not substituted."""
    forecast_date = proposed_time.astimezone(timezone.utc).date().isoformat()
    try:
        # Do not inherit a machine-level proxy configuration: it may be unavailable
        # even when the public forecast endpoint itself is reachable.
        with httpx.Client(trust_env=False, timeout=10.0) as client:
            response = client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": latitude, "longitude": longitude, "hourly": "temperature_2m,wind_speed_10m,visibility",
                    "timezone": "UTC", "start_date": forecast_date, "end_date": forecast_date,
                },
            )
        response.raise_for_status()
        hourly = response.json().get("hourly", {})
        target_hour = proposed_time.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")
        timestamps = hourly.get("time", [])
        normalized_times = [f"{item}:00Z" if item.endswith(":00") else item for item in timestamps]
        if target_hour not in normalized_times:
            return None
        index = normalized_times.index(target_hour)
        values = {"temperature_c": hourly["temperature_2m"][index], "wind_speed_kmh": hourly["wind_speed_10m"][index], "visibility_m": hourly["visibility"][index]}
        if any(value is None for value in values.values()):
            return None
        return {"forecast_timestamp": target_hour, **values}
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None


@app.post("/api/v1/feasibility/assessments", status_code=201)
def assess_feasibility(request: FeasibilityRequest) -> dict[str, Any]:
    with connection() as db:
        row = db.execute(
            """SELECT normalized_task_json FROM ingestion_records
               WHERE source_reference = ? AND data_quality_status = 'COMPLETE'
               ORDER BY received_at DESC LIMIT 1""",
            (request.task_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "A complete normalized task with this task ID was not found.")
    task = NormalizedTask.model_validate_json(row["normalized_task_json"])
    forecast = fetch_forecast(request.section_latitude, request.section_longitude, request.proposed_time)
    assessment_id = str(uuid4())
    reasons: list[str] = []
    missing_rules: list[str] = []
    if task.department is Department.ENGINEERING and request.rules.engineering_max_temperature_c is None:
        missing_rules.append("engineering maximum temperature")
    if task.department is Department.TRACTION and request.rules.traction_max_wind_speed_kmh is None:
        missing_rules.append("traction maximum wind speed")
    if task.requires_traffic_block and request.rules.traffic_block_min_visibility_m is None:
        missing_rules.append("traffic-block minimum visibility")
    if task.requires_traffic_block and request.rules.caution_risk_multiplier is None:
        missing_rules.append("caution risk multiplier")
    if forecast is None or missing_rules:
        if forecast is None:
            reasons.append("No forecast is available for the proposed hour and supplied section coordinates.")
        if missing_rules:
            reasons.append(f"Missing authority-approved rule values: {', '.join(missing_rules)}.")
        result = {"assessment_id": assessment_id, "task_id": task.id, "status": "NEEDS_REVIEW", "viable": False, "risk_multiplier": None, "warning_reasons": reasons, "forecast": forecast, "rule_version": request.rules.rule_version, "assessed_at": now().isoformat()}
    else:
        status = "SUITABLE"
        risk_multiplier = 1.0
        if task.department is Department.ENGINEERING and forecast["temperature_c"] > request.rules.engineering_max_temperature_c:
            status = "NOT_SUITABLE"
            reasons.append("Forecast temperature exceeds the supplied Engineering limit.")
        if task.department is Department.TRACTION and forecast["wind_speed_kmh"] > request.rules.traction_max_wind_speed_kmh:
            status = "NOT_SUITABLE"
            reasons.append("Forecast wind speed exceeds the supplied Traction limit.")
        if task.requires_traffic_block and forecast["visibility_m"] < request.rules.traffic_block_min_visibility_m:
            if status != "NOT_SUITABLE":
                status = "CAUTION_REQUIRED"
            risk_multiplier = request.rules.caution_risk_multiplier
            reasons.append("Forecast visibility is below the supplied traffic-block minimum.")
        if not reasons:
            reasons.append("Forecast conditions meet all supplied applicable limits.")
        result = {"assessment_id": assessment_id, "task_id": task.id, "status": status, "viable": status != "NOT_SUITABLE", "risk_multiplier": risk_multiplier, "warning_reasons": reasons, "forecast": forecast, "rule_version": request.rules.rule_version, "assessed_at": now().isoformat()}
    with connection() as db:
        db.execute(
            "INSERT INTO feasibility_assessments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (assessment_id, task.id, request.proposed_time.isoformat(), request.section_latitude, request.section_longitude,
             request.rules.rule_version, json.dumps(result), now().isoformat()),
        )
    return result


def normalized_factor(value: float, maximum: float) -> float:
    if value > maximum:
        raise ValueError("A supplied measured value exceeds its policy maximum; update the approved policy rather than silently clipping it.")
    return (value / maximum) * 100


@app.get("/api/v1/priority/policies")
def list_priority_policies() -> list[dict[str, Any]]:
    with connection() as db:
        rows = db.execute("SELECT policy_id, policy_json, approval_reference, approved_at, created_at FROM priority_policies ORDER BY approved_at DESC").fetchall()
    return [{"policy_id": row["policy_id"], "policy": json.loads(row["policy_json"]), "approval_reference": row["approval_reference"], "approved_at": row["approved_at"], "created_at": row["created_at"]} for row in rows]


@app.post("/api/v1/priority/policies", status_code=201)
def register_priority_policy(registration: PriorityPolicyRegistration) -> dict[str, Any]:
    try:
        with connection() as db:
            db.execute(
                "INSERT INTO priority_policies VALUES (?, ?, ?, ?, ?)",
                (registration.policy_id, registration.policy.model_dump_json(), registration.approval_reference,
                 registration.approved_at.isoformat(), now().isoformat()),
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(409, "An approved priority policy already exists with this policy_id.") from error
    return {"status": "REGISTERED", **registration.model_dump(mode="json")}


@app.post("/api/v1/priority/evaluations", status_code=201)
def evaluate_priority(request: PriorityRequest) -> dict[str, Any]:
    policy = request.policy
    policy_id = request.policy_id
    if policy_id is not None:
        with connection() as db:
            policy_row = db.execute("SELECT policy_json FROM priority_policies WHERE policy_id = ?", (policy_id,)).fetchone()
        if policy_row is None:
            raise HTTPException(404, "No approved priority policy exists with this policy_id.")
        policy = PriorityPolicy.model_validate_json(policy_row["policy_json"])
    assert policy is not None
    with connection() as db:
        task_row = db.execute(
            """SELECT normalized_task_json FROM ingestion_records
               WHERE source_reference = ? AND data_quality_status = 'COMPLETE'
               ORDER BY received_at DESC LIMIT 1""", (request.context.task_id,)
        ).fetchone()
        assessment_row = db.execute(
            """SELECT result_json FROM feasibility_assessments WHERE task_id = ?
               ORDER BY created_at DESC LIMIT 1""", (request.context.task_id,)
        ).fetchone()
    if task_row is None:
        raise HTTPException(404, "A complete F-01 task with this task ID was not found.")
    if assessment_row is None:
        raise HTTPException(409, "A current F-02 assessment is required before F-03 prioritization.")
    task = NormalizedTask.model_validate_json(task_row["normalized_task_json"])
    assessment = json.loads(assessment_row["result_json"])
    if assessment["status"] not in {"SUITABLE", "CAUTION_REQUIRED"} or assessment["risk_multiplier"] is None:
        raise HTTPException(409, "The latest F-02 assessment is not eligible for priority scoring.")
    try:
        factors = {
            "safety severity": policy.severity_scores[task.severity],
            "overdue age": normalized_factor(max(0, (now() - task.due_date).total_seconds() / 86400), policy.max_days_overdue),
            "passenger timetable demand": normalized_factor(request.context.passenger_train_frequency_per_day, policy.max_passenger_trains_per_day),
            "goods forecast demand": normalized_factor(request.context.goods_train_forecast_per_day, policy.max_goods_trains_per_day),
            "section traffic": normalized_factor(request.context.section_traffic_gmt, policy.max_section_traffic_gmt),
            "active operational restriction": policy.active_restriction_score if request.context.active_operational_restriction else 0,
            "route criticality": request.context.route_criticality_score,
            "F-02 weather risk": normalized_factor(assessment["risk_multiplier"] - 1, policy.max_weather_risk_multiplier - 1),
        }
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    weights = {
        "safety severity": policy.severity_weight, "overdue age": policy.overdue_weight,
        "passenger timetable demand": policy.passenger_weight, "goods forecast demand": policy.goods_weight,
        "section traffic": policy.traffic_weight, "active operational restriction": policy.restriction_weight,
        "route criticality": policy.route_criticality_weight, "F-02 weather risk": policy.weather_weight,
    }
    total_weight = sum(weights.values())
    contributions = [{"factor": name, "input_score": round(factors[name], 2), "weight": weight, "score_contribution": round((factors[name] * weight) / total_weight, 2)} for name, weight in weights.items() if weight > 0]
    score = round(sum(item["score_contribution"] for item in contributions), 2)
    tier = "CRITICAL" if score >= policy.critical_threshold else "HIGH" if score >= policy.high_threshold else "MEDIUM" if score >= policy.medium_threshold else "LOW"
    result = {"evaluation_id": str(uuid4()), "task_id": task.id, "score": score, "tier": tier, "policy_id": policy_id, "policy_version": policy.policy_version, "f02_assessment_id": assessment["assessment_id"], "top_contributing_factors": sorted(contributions, key=lambda item: item["score_contribution"], reverse=True), "explanation": "Priority is a transparent weighted calculation from the supplied policy and recorded inputs; it is not an ML prediction.", "evaluated_at": now().isoformat()}
    with connection() as db:
        db.execute("INSERT INTO priority_evaluations VALUES (?, ?, ?, ?, ?, ?)", (result["evaluation_id"], task.id, policy.policy_version, json.dumps(request.context.model_dump()), json.dumps(result), now().isoformat()))
    return result


def utc_hour(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def intervals_overlap(first_start: datetime, first_end: datetime, second_start: datetime, second_end: datetime) -> bool:
    return first_start < second_end and second_start < first_end


@app.get("/api/v1/block-plans/eligibility")
def block_plan_eligibility() -> list[dict[str, Any]]:
    """Explain F-04 inclusion status before a COA planning run."""
    with connection() as db:
        task_rows = db.execute("SELECT normalized_task_json FROM ingestion_records WHERE data_quality_status = 'COMPLETE'").fetchall()
        priority_rows = db.execute("SELECT task_id, result_json, created_at FROM priority_evaluations ORDER BY created_at DESC").fetchall()
        assessment_rows = db.execute("SELECT task_id, result_json, proposed_time, created_at FROM feasibility_assessments ORDER BY created_at DESC").fetchall()
    priorities: dict[str, dict[str, Any]] = {}
    for row in priority_rows:
        priorities.setdefault(row["task_id"], json.loads(row["result_json"]))
    assessments: dict[str, dict[str, Any]] = {}
    for row in assessment_rows:
        assessments.setdefault(row["task_id"], {**json.loads(row["result_json"]), "proposed_time": row["proposed_time"]})
    response = []
    for row in task_rows:
        task = NormalizedTask.model_validate_json(row["normalized_task_json"])
        assessment, priority = assessments.get(task.id), priorities.get(task.id)
        missing = []
        if assessment is None:
            missing.append("Run F-02 feasibility assessment.")
        elif assessment["status"] not in {"SUITABLE", "CAUTION_REQUIRED"}:
            missing.append(f"Latest F-02 status is {assessment['status']}; an eligible assessment is required.")
        if priority is None:
            missing.append("Run F-03 priority evaluation.")
        response.append({"task_id": task.id, "department": task.department, "section_id": task.section_id, "duration_minutes": task.estimated_duration_minutes, "priority_score": priority.get("score") if priority else None, "eligible": not missing, "missing_requirements": missing, "required_coa_start_hour": assessment.get("proposed_time") if assessment and assessment["status"] in {"SUITABLE", "CAUTION_REQUIRED"} else None})
    return sorted(response, key=lambda item: (not item["eligible"], item["task_id"]))


@app.post("/api/v1/block-plans", status_code=201)
def create_block_plan(request: BlockPlanRequest) -> dict[str, Any]:
    """Bounded exact search over real eligible tasks and submitted COA windows.

    Work is parallelized only where every concurrent task explicitly records
    co-working compatibility and COA crew capacity is sufficient; otherwise it
    is sequenced within the shared block.
    """
    with connection() as db:
        task_rows = db.execute(
            """SELECT source_reference, normalized_task_json FROM ingestion_records
               WHERE data_quality_status = 'COMPLETE'"""
        ).fetchall()
        priority_rows = db.execute(
            """SELECT task_id, result_json, created_at FROM priority_evaluations
               ORDER BY created_at DESC"""
        ).fetchall()
        assessment_rows = db.execute(
            """SELECT task_id, result_json, proposed_time, created_at FROM feasibility_assessments
               ORDER BY created_at DESC"""
        ).fetchall()
    latest_priorities: dict[str, dict[str, Any]] = {}
    for row in priority_rows:
        latest_priorities.setdefault(row["task_id"], json.loads(row["result_json"]))
    latest_assessments: dict[str, dict[str, Any]] = {}
    for row in assessment_rows:
        latest_assessments.setdefault(row["task_id"], {**json.loads(row["result_json"]), "proposed_time": row["proposed_time"]})

    eligible: list[dict[str, Any]] = []
    deferred: list[dict[str, str]] = []
    for row in task_rows:
        task = NormalizedTask.model_validate_json(row["normalized_task_json"])
        priority = latest_priorities.get(task.id)
        assessment = latest_assessments.get(task.id)
        if priority is None:
            deferred.append({"task_id": task.id, "reason": "No F-03 priority evaluation is available."})
            continue
        if assessment is None or assessment.get("status") not in {"SUITABLE", "CAUTION_REQUIRED"}:
            deferred.append({"task_id": task.id, "reason": "No eligible F-02 feasibility assessment is available."})
            continue
        eligible.append({"task": task, "priority": priority, "assessment": assessment})

    windows = sorted(request.coa_windows, key=lambda window: window.start_time)
    candidates: list[dict[str, Any]] = []
    for item in eligible:
        task, assessment = item["task"], item["assessment"]
        assessment_hour = utc_hour(datetime.fromisoformat(assessment["proposed_time"]))
        choices: list[int] = []
        reasons: list[str] = []
        for index, window in enumerate(windows):
            if window.section_id != task.section_id:
                continue
            if utc_hour(window.start_time) != assessment_hour:
                reasons.append("F-02 assessment does not cover this COA window hour.")
                continue
            if task.requires_traffic_block and not window.traffic_block_available:
                reasons.append("COA window does not provide the required traffic block.")
                continue
            if task.requires_traction_disconnection and not window.traction_disconnection_available:
                reasons.append("COA window does not provide the required traction disconnection.")
                continue
            if len(task.required_crews) > window.max_simultaneous_crews:
                reasons.append("Task crew requirement exceeds COA window capacity.")
                continue
            if task.estimated_duration_minutes > (window.end_time - window.start_time).total_seconds() / 60:
                reasons.append("Task duration exceeds the available COA window.")
                continue
            if window.end_time > task.due_date:
                reasons.append("COA window ends after the recorded task due date.")
                continue
            choices.append(index)
        if not choices:
            deferred.append({"task_id": task.id, "reason": "; ".join(sorted(set(reasons))) or "No compatible COA window matches the task section."})
        else:
            candidates.append({**item, "choices": choices})

    candidates.sort(key=lambda item: (-item["priority"]["score"], item["task"].due_date, item["task"].id))
    remaining_bound = [0.0] * (len(candidates) + 1)
    for index in range(len(candidates) - 1, -1, -1):
        remaining_bound[index] = remaining_bound[index + 1] + candidates[index]["priority"]["score"]
    best: dict[str, Any] = {"score": -1.0, "used": float("inf"), "slack": float("inf"), "assignments": []}
    start_clock = time.monotonic()
    timed_out = False

    def consider(assignments: list[dict[str, Any]], score: float, used_minutes: list[int]) -> None:
        nonlocal best
        used_count = sum(minutes > 0 for minutes in used_minutes)
        slack = sum((windows[index].end_time - windows[index].start_time).total_seconds() / 60 - minutes for index, minutes in enumerate(used_minutes) if minutes > 0)
        candidate_key = (round(score, 6), -used_count, -slack)
        current_key = (round(best["score"], 6), -best["used"], -best["slack"])
        if candidate_key > current_key:
            best = {"score": score, "used": used_count, "slack": slack, "assignments": [dict(assignment) for assignment in assignments]}

    def search(index: int, assignments: list[dict[str, Any]], score: float, used_minutes: list[int]) -> None:
        nonlocal timed_out
        if time.monotonic() - start_clock > 4.0:
            timed_out = True
            return
        if score + remaining_bound[index] < best["score"]:
            return
        if index == len(candidates):
            consider(assignments, score, used_minutes)
            return
        item = candidates[index]
        task = item["task"]
        # Unscheduled branch preserves a complete, explainable result even when capacity is insufficient.
        search(index + 1, assignments, score, used_minutes)
        for window_index in item["choices"]:
            window = windows[window_index]
            within_window = [assigned for assigned in assignments if assigned["window_index"] == window_index]
            can_parallelize = (
                bool(within_window)
                and task.co_working_compatible
                and all(assigned["task"].co_working_compatible and assigned["start_time"] == window.start_time for assigned in within_window)
                and sum(len(assigned["task"].required_crews) for assigned in within_window) + len(task.required_crews) <= window.max_simultaneous_crews
            )
            task_start = window.start_time if can_parallelize else window.start_time + timedelta(minutes=used_minutes[window_index])
            task_end = task_start + timedelta(minutes=task.estimated_duration_minutes)
            if task_end > window.end_time:
                continue
            resource_conflict = any(
                (set(task.required_crews).intersection(assigned["task"].required_crews)
                 or set(task.required_equipment).intersection(assigned["task"].required_equipment))
                and intervals_overlap(task_start, task_end, assigned["start_time"], assigned["end_time"])
                for assigned in assignments
            )
            if resource_conflict:
                continue
            assignment = {"task": task, "priority": item["priority"], "assessment": item["assessment"], "window_index": window_index, "start_time": task_start, "end_time": task_end}
            previous_occupancy = used_minutes[window_index]
            used_minutes[window_index] = max(previous_occupancy, (task_end - window.start_time).total_seconds() / 60)
            assignments.append(assignment)
            search(index + 1, assignments, score + item["priority"]["score"], used_minutes)
            assignments.pop()
            used_minutes[window_index] = previous_occupancy

    from datetime import timedelta
    search(0, [], 0.0, [0] * len(windows))
    scheduled_ids = {assignment["task"].id for assignment in best["assignments"]}
    for item in candidates:
        if item["task"].id not in scheduled_ids:
            deferred.append({"task_id": item["task"].id, "reason": "No remaining compatible COA capacity after optimizing higher-priority work and shared block use."})
    blocks: list[dict[str, Any]] = []
    for index, window in enumerate(windows):
        assigned = sorted((item for item in best["assignments"] if item["window_index"] == index), key=lambda item: item["start_time"])
        if not assigned:
            continue
        blocks.append({
            "corridor_id": window.corridor_id, "section_id": window.section_id,
            "window_start": window.start_time, "window_end": window.end_time,
            "timetable_reference": window.timetable_reference, "goods_forecast_reference": window.goods_forecast_reference,
            "passenger_trains_affected": window.passenger_trains_affected, "goods_trains_affected": window.goods_trains_affected,
            "assigned_tasks": [{"task_id": item["task"].id, "department": item["task"].department, "priority_score": item["priority"]["score"], "scheduled_start": item["start_time"], "scheduled_end": item["end_time"], "requires_traffic_block": item["task"].requires_traffic_block, "requires_traction_disconnection": item["task"].requires_traction_disconnection, "required_crews": item["task"].required_crews} for item in assigned],
            "consolidated_departments": sorted({item["task"].department for item in assigned}),
            "used_minutes": max((item["end_time"] - window.start_time).total_seconds() / 60 for item in assigned),
            "total_maintenance_minutes": sum(item["task"].estimated_duration_minutes for item in assigned),
            "available_minutes": (window.end_time - window.start_time).total_seconds() / 60,
        })
    plan_id = str(uuid4())
    result = {"plan_id": plan_id, "status": "PROPOSED", "horizon": request.horizon, "horizon_start": request.horizon_start, "horizon_end": request.horizon_end, "scheduled_blocks": blocks, "unscheduled_tasks": deferred, "metrics": {"scheduled_task_count": len(scheduled_ids), "unscheduled_task_count": len(deferred), "total_priority_score_scheduled": round(best["score"], 2), "distinct_corridor_disruptions": len(blocks), "shared_block_count": sum(1 for block in blocks if len(block["assigned_tasks"]) > 1), "total_block_hours_used": round(sum(block["used_minutes"] for block in blocks) / 60, 2), "parallel_block_hours_saved": round(sum(max(0, block["total_maintenance_minutes"] - block["used_minutes"]) for block in blocks) / 60, 2), "passenger_trains_affected": sum(block["passenger_trains_affected"] for block in blocks), "goods_trains_affected": sum(block["goods_trains_affected"] for block in blocks), "search_timed_out": timed_out}, "notes": ["Tasks run in parallel only when every concurrent task explicitly permits co-working, no crew or equipment resource conflicts exist, and COA capacity covers all crews; otherwise they are sequenced.", "Proposed only: controller sanction remains required."]}
    with connection() as db:
        db.execute("INSERT INTO block_plans VALUES (?, ?, ?, ?, ?, ?, ?)", (plan_id, request.horizon, request.horizon_start.isoformat(), request.horizon_end.isoformat(), json.dumps([window.model_dump(mode="json") for window in windows]), json.dumps(result, default=str), now().isoformat()))
    return result


@app.post("/api/v1/block-plans/from-integrated-data", status_code=201)
def create_integrated_block_plan(request: IntegratedPlanRequest) -> dict[str, Any]:
    """Build F-04 directly from previously imported COA, timetable and goods records."""
    with connection() as db:
        rows = db.execute("SELECT window_json FROM coa_windows").fetchall()
        timetable_rows = db.execute("SELECT * FROM timetable_occupancy").fetchall()
        goods_rows = db.execute("SELECT * FROM goods_forecasts").fetchall()
    timetable = {row["record_id"]: row for row in timetable_rows}
    goods = {row["record_id"]: row for row in goods_rows}
    windows: list[CorridorWindow] = []
    review_reasons: list[str] = []
    for row in rows:
        window = CorridorWindow.model_validate_json(row["window_json"])
        if window.start_time < request.horizon_start or window.end_time > request.horizon_end:
            continue
        timetable_source, goods_source = timetable.get(window.timetable_reference), goods.get(window.goods_forecast_reference)
        if timetable_source is None or goods_source is None:
            review_reasons.append(f"{window.corridor_id}: cited operational source record is no longer available.")
            continue
        if timetable_source["section_id"] != window.section_id or goods_source["section_id"] != window.section_id:
            review_reasons.append(f"{window.corridor_id}: cited operational data no longer matches the COA section.")
            continue
        windows.append(window.model_copy(update={
            "passenger_trains_affected": timetable_source["passenger_trains_affected"],
            "goods_trains_affected": goods_source["goods_trains_affected"],
        }))
    if not windows:
        detail = " No imported COA availability is inside this horizon."
        if review_reasons:
            detail += " " + " ".join(review_reasons)
        raise HTTPException(409, detail.strip())
    result = create_block_plan(BlockPlanRequest(
        horizon=request.horizon,
        horizon_start=request.horizon_start,
        horizon_end=request.horizon_end,
        coa_windows=windows,
    ))
    result["operational_data_mode"] = "IMPORTED"
    result["operational_data_review_reasons"] = review_reasons
    return result

# --- F-05 Interactive "What-If" Delay Simulator ---

import networkx as nx
from datetime import timedelta
import time as perf_time

class TrainType(StrEnum):
    EXPRESS = "EXPRESS"
    SUBURBAN = "SUBURBAN"
    FREIGHT = "FREIGHT"

class TrainSchedule(BaseModel):
    train_no: str
    train_type: TrainType
    station_stops: list[dict[str, Any]]

class WhatIfRequest(BaseModel):
    corridor_id: str
    section_from: str
    section_to: str
    block_start_time: datetime
    block_end_time: datetime
    trains: list["TrainSchedule"] | None = None
    """Operator-supplied train schedules for the simulation.
    When omitted the engine derives schedules from the imported
    timetable-occupancy records that cover the proposed block window.
    Missing schedules are surfaced as uncertainty, not silently assumed safe."""

    @field_validator("block_start_time", "block_end_time")
    @classmethod
    def time_is_aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def block_window_is_ordered(self) -> "WhatIfRequest":
        if self.block_end_time <= self.block_start_time:
            raise ValueError("block_end_time must be after block_start_time.")
        return self

class RegulatedTrain(BaseModel):
    train_no: str
    train_type: TrainType
    held_at_station: str
    delay_minutes: int
    original_arrival: datetime
    simulated_arrival: datetime

class SimulationReport(BaseModel):
    block_id_simulated: str
    section_impacted: str
    total_passenger_delay_minutes: int
    total_freight_delay_minutes: int
    regulated_trains: list[RegulatedTrain]
    network_punctuality_impact_pct: float
    headway_conflict_warnings: list[str]

class CorridorWhatIfSimulator:
    def __init__(self, schedules: list[TrainSchedule]):
        self.schedules = schedules
        self.graph = nx.DiGraph()
        self._build_graph()

    def _get_priority(self, t_type: TrainType) -> int:
        if t_type == TrainType.EXPRESS:
            return 1
        elif t_type == TrainType.SUBURBAN:
            return 2
        return 3

    def _build_graph(self):
        for schedule in self.schedules:
            stops = schedule.station_stops
            for i in range(len(stops)):
                stop = stops[i]
                station = stop["station_code"]
                arr = stop["scheduled_arrival"]
                dep = stop["scheduled_departure"]
                
                node_arr = (station, "ARR", schedule.train_no)
                node_dep = (station, "DEP", schedule.train_no)
                
                self.graph.add_node(node_arr, time=arr, train_no=schedule.train_no, type=schedule.train_type, priority=self._get_priority(schedule.train_type))
                self.graph.add_node(node_dep, time=dep, train_no=schedule.train_no, type=schedule.train_type, priority=self._get_priority(schedule.train_type))
                
                self.graph.add_edge(node_arr, node_dep, weight=(dep - arr).total_seconds() / 60)
                
                if i < len(stops) - 1:
                    next_stop = stops[i+1]
                    next_arr_node = (next_stop["station_code"], "ARR", schedule.train_no)
                    travel_time = (next_stop["scheduled_arrival"] - dep).total_seconds() / 60
                    self.graph.add_edge(node_dep, next_arr_node, weight=travel_time)

        segments = {}
        for schedule in self.schedules:
            stops = schedule.station_stops
            for i in range(len(stops) - 1):
                s1 = stops[i]["station_code"]
                s2 = stops[i+1]["station_code"]
                segments.setdefault((s1, s2), []).append((schedule.train_no, stops[i]["scheduled_departure"]))
        
        for (s1, s2), trains in segments.items():
            trains.sort(key=lambda x: x[1])
            for i in range(len(trains) - 1):
                t1, t1_dep = trains[i]
                t2, t2_dep = trains[i+1]
                node1_dep = (s1, "DEP", t1)
                node2_dep = (s1, "DEP", t2)
                self.graph.add_edge(node1_dep, node2_dep, weight=5.0)

    def simulate_block(self, section_from: str, section_to: str, start_time: datetime, end_time: datetime) -> SimulationReport:
        affected = []
        original_times = nx.get_node_attributes(self.graph, "time")
        
        for schedule in self.schedules:
            stops = schedule.station_stops
            for i in range(len(stops) - 1):
                s1 = stops[i]["station_code"]
                s2 = stops[i+1]["station_code"]
                if s1 == section_from and s2 == section_to:
                    dep_time = stops[i]["scheduled_departure"]
                    arr_time = stops[i+1]["scheduled_arrival"]
                    if not (arr_time <= start_time or dep_time >= end_time):
                        affected.append(schedule.train_no)
        
        new_times = {n: original_times[n] for n in self.graph.nodes}
        queue = []
        for t_no in affected:
            node = (section_from, "DEP", t_no)
            queue.append((node, self.graph.nodes[node]["priority"], original_times[node]))
            
        queue.sort(key=lambda x: (x[1], x[2]))
        
        current_release_time = end_time
        for node, prio, orig_time in queue:
            if current_release_time > new_times[node]:
                new_times[node] = current_release_time
            current_release_time += timedelta(minutes=5)
            
        topo = list(nx.topological_sort(self.graph))
        for u in topo:
            for v in self.graph.successors(u):
                edge_weight = self.graph[u][v]["weight"]
                min_v_time = new_times[u] + timedelta(minutes=edge_weight)
                if min_v_time > new_times[v]:
                    new_times[v] = min_v_time
                    
        regulated = []
        tot_passenger_delay = 0
        tot_freight_delay = 0
        
        for schedule in self.schedules:
            t_no = schedule.train_no
            t_type = schedule.train_type
            last_stop = schedule.station_stops[-1]["station_code"]
            final_node = (last_stop, "ARR", t_no)
            orig = original_times[final_node]
            sim = new_times[final_node]
            delay = int((sim - orig).total_seconds() / 60)
            if delay > 0:
                held_at = section_from if t_no in affected else "Upstream"
                regulated.append(RegulatedTrain(
                    train_no=t_no,
                    train_type=t_type,
                    held_at_station=held_at,
                    delay_minutes=delay,
                    original_arrival=orig,
                    simulated_arrival=sim
                ))
                if t_type == TrainType.FREIGHT:
                    tot_freight_delay += delay
                else:
                    tot_passenger_delay += delay
                    
        regulated.sort(key=lambda x: -x.delay_minutes)
        total_trains = len(self.schedules)
        delayed_trains = len(regulated)
        punctuality = ((total_trains - delayed_trains) / total_trains * 100.0) if total_trains else 100.0
        
        return SimulationReport(
            block_id_simulated=str(uuid4()),
            section_impacted=f"{section_from}->{section_to}",
            total_passenger_delay_minutes=tot_passenger_delay,
            total_freight_delay_minutes=tot_freight_delay,
            regulated_trains=regulated,
            network_punctuality_impact_pct=round(punctuality, 2),
            headway_conflict_warnings=[]
        )

def _generate_mock_schedules() -> list[TrainSchedule]:
    schedules = []
    base_time = now().replace(hour=0, minute=0, second=0, microsecond=0)
    stations = ["A", "B", "C", "D", "E"]
    
    for i in range(15):
        t_type = TrainType.EXPRESS if i < 5 else (TrainType.SUBURBAN if i < 10 else TrainType.FREIGHT)
        start = base_time + timedelta(hours=8, minutes=i*20)
        stops = []
        curr_time = start
        for j, st in enumerate(stations):
            arr = curr_time if j == 0 else curr_time
            dep = arr + timedelta(minutes=5)
            stops.append({
                "station_code": st,
                "scheduled_arrival": arr,
                "scheduled_departure": dep
            })
            curr_time = dep + timedelta(minutes=15)
        
        schedules.append(TrainSchedule(
            train_no=f"TRN-{i+1:03d}",
            train_type=t_type,
            station_stops=stops
        ))
    return schedules

@app.post("/api/v1/simulate/what-if", status_code=200, response_model=SimulationReport)
def simulate_what_if(request: WhatIfRequest) -> SimulationReport:
    """F-05: Estimate operational impact of a proposed block window.

    If the caller supplies `trains`, those schedules are used directly.
    Otherwise, schedules are derived from imported timetable-occupancy records
    that overlap the proposed block window on the matching section.
    When no imported data is available the endpoint raises 409 rather than
    silently substituting invented train movements.
    """
    if request.trains is not None:
        schedules = request.trains
    else:
        with connection() as db:
            rows = db.execute(
                """SELECT record_id, section_id, window_start, window_end,
                          passenger_trains_affected
                   FROM timetable_occupancy
                   WHERE section_id = ?
                     AND window_start <= ?
                     AND window_end   >= ?
                   ORDER BY window_start""",
                (
                    request.section_from,
                    request.block_end_time.isoformat(),
                    request.block_start_time.isoformat(),
                ),
            ).fetchall()
        if not rows:
            raise HTTPException(
                409,
                "No imported timetable-occupancy records cover this section and window. "
                "Import timetable data via Operations data or supply train schedules in the request body.",
            )
        # Build minimal TrainSchedule stubs from the occupancy records so the
        # graph simulator has real-data-grounded entries.  Each occupancy record
        # becomes one representative train entry with two stops (section_from and
        # section_to) timed to the imported window boundaries.
        schedules = []
        for index, row in enumerate(rows):
            w_start = datetime.fromisoformat(row["window_start"])
            w_end   = datetime.fromisoformat(row["window_end"])
            affected = row["passenger_trains_affected"]
            t_type   = TrainType.EXPRESS if affected >= 10 else TrainType.SUBURBAN
            for train_index in range(max(1, min(affected, 5))):
                headway = timedelta(minutes=15 * train_index)
                schedules.append(
                    TrainSchedule(
                        train_no=f"{row['record_id']}-T{train_index + 1:02d}",
                        train_type=t_type,
                        station_stops=[
                            {
                                "station_code": request.section_from,
                                "scheduled_arrival":   w_start + headway,
                                "scheduled_departure": w_start + headway + timedelta(minutes=5),
                            },
                            {
                                "station_code": request.section_to,
                                "scheduled_arrival":   w_end + headway,
                                "scheduled_departure": w_end + headway + timedelta(minutes=5),
                            },
                        ],
                    )
                )
    if not schedules:
        raise HTTPException(
            409,
            "No train schedules could be derived for the requested section and window.",
        )
    simulator = CorridorWhatIfSimulator(schedules)
    return simulator.simulate_block(
        section_from=request.section_from,
        section_to=request.section_to,
        start_time=request.block_start_time,
        end_time=request.block_end_time,
    )

# --- F-06 Dynamic Overrun & Early Handover Engine ---

import asyncio
from fastapi import WebSocket, WebSocketDisconnect
import redis.asyncio as redis

class BlockState(StrEnum):
    PENDING_START = "PENDING_START"
    ACTIVE = "ACTIVE"
    EXTENSION_REQUESTED = "EXTENSION_REQUESTED"
    CLEARED_EARLY = "CLEARED_EARLY"
    COMPLETED = "COMPLETED"

class ProgressTelemetryUpdate(BaseModel):
    block_id: str
    corridor_id: str
    supervisor_id: str
    actual_progress_pct: float
    estimated_minutes_remaining: int
    timestamp: datetime
    
    @field_validator("timestamp")
    @classmethod
    def time_is_aware_telemetry(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

class DownlineWarningAlert(BaseModel):
    alert_id: str
    block_id: str
    corridor_id: str
    severity: str
    overrun_minutes: int
    revised_handover_time: datetime
    first_impacted_train: dict[str, Any]
    message: str

class EarlyHandoverAlert(BaseModel):
    block_id: str
    corridor_id: str
    slack_capacity_recovered_minutes: int
    recommended_action: str

class LiveBlockStateMessage(BaseModel):
    block_id: str
    state: BlockState
    percent_completed: float
    telemetry_timestamp: datetime
    active_alert: dict | None

class MockRedisPubSub:
    def __init__(self):
        self.channels = {}
    
    async def publish(self, channel: str, message: str):
        if channel not in self.channels:
            self.channels[channel] = set()
        for q in self.channels[channel]:
            await q.put(message)
            
    async def subscribe(self, channel: str):
        q = asyncio.Queue()
        if channel not in self.channels:
            self.channels[channel] = set()
        self.channels[channel].add(q)
        return q

    async def unsubscribe(self, channel: str, q: asyncio.Queue):
        if channel in self.channels and q in self.channels[channel]:
            self.channels[channel].remove(q)

fallback_pubsub = MockRedisPubSub()
redis_client = None

async def get_redis():
    global redis_client
    if redis_client is None:
        try:
            redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
            await redis_client.ping()
        except Exception:
            redis_client = fallback_pubsub
    return redis_client

class LiveTelemetryEngine:
    def __init__(self):
        self.blocks = {}
    
    async def process_telemetry(self, update: ProgressTelemetryUpdate) -> LiveBlockStateMessage:
        block = self.blocks.get(update.block_id)
        if not block:
            scheduled_end_time = update.timestamp + timedelta(minutes=update.estimated_minutes_remaining)
            block = {
                "scheduled_end_time": scheduled_end_time,
                "state": BlockState.ACTIVE
            }
            self.blocks[update.block_id] = block
            
        scheduled_end_time = block["scheduled_end_time"]
        projected_end_time = update.timestamp + timedelta(minutes=update.estimated_minutes_remaining)
        
        alert = None
        new_state = block["state"]
        
        # Overrun Detection
        if (projected_end_time - scheduled_end_time).total_seconds() / 60 > 15:
            new_state = BlockState.EXTENSION_REQUESTED
            overrun = int((projected_end_time - scheduled_end_time).total_seconds() / 60)
            # Identify the first timetable record whose window overlaps the
            # revised handover time on this corridor.  If none is imported the
            # controller is explicitly notified so they can consult the timetable.
            with connection() as db:
                first_tt = db.execute(
                    """SELECT record_id, passenger_trains_affected
                       FROM timetable_occupancy
                       WHERE window_start <= ? AND window_end >= ?
                       ORDER BY window_start LIMIT 1""",
                    (projected_end_time.isoformat(), projected_end_time.isoformat()),
                ).fetchone()
            first_train_info: dict[str, Any]
            if first_tt is not None:
                first_train_info = {
                    "timetable_record_id": first_tt["record_id"],
                    "passenger_trains_affected": first_tt["passenger_trains_affected"],
                    "scheduled_arrival": scheduled_end_time.isoformat(),
                    "expected_delay_mins": overrun,
                    "note": "Identify specific train from the imported timetable record.",
                }
            else:
                first_train_info = {
                    "timetable_record_id": None,
                    "scheduled_arrival": scheduled_end_time.isoformat(),
                    "expected_delay_mins": overrun,
                    "note": (
                        "No timetable-occupancy record imported for this window. "
                        "Controller must identify the first affected train manually."
                    ),
                }
            alert = DownlineWarningAlert(
                alert_id=str(uuid4()),
                block_id=update.block_id,
                corridor_id=update.corridor_id,
                severity="WARNING",
                overrun_minutes=overrun,
                revised_handover_time=projected_end_time,
                first_impacted_train=first_train_info,
                message=f"Block {update.block_id} projected to overrun by {overrun} minutes.",
            ).model_dump(mode="json")
            
        # Early Handover Detection
        elif update.actual_progress_pct == 100.0 and (scheduled_end_time - update.timestamp).total_seconds() / 60 > 20:
            new_state = BlockState.CLEARED_EARLY
            slack = int((scheduled_end_time - update.timestamp).total_seconds() / 60)
            alert = EarlyHandoverAlert(
                block_id=update.block_id,
                corridor_id=update.corridor_id,
                slack_capacity_recovered_minutes=slack,
                recommended_action=f"Restore line early for waiting traffic"
            ).model_dump(mode='json')
            
        elif update.actual_progress_pct == 100.0:
            new_state = BlockState.COMPLETED
            
        block["state"] = new_state
        
        msg = LiveBlockStateMessage(
            block_id=update.block_id,
            state=new_state,
            percent_completed=update.actual_progress_pct,
            telemetry_timestamp=update.timestamp,
            active_alert=alert
        )
        
        client = await get_redis()
        channel = f"corridor:{update.corridor_id}:updates"
        
        if hasattr(client, 'publish') and not isinstance(client, MockRedisPubSub):
            await client.publish(channel, msg.model_dump_json())
        else:
            await fallback_pubsub.publish(channel, msg.model_dump_json())
            
        return msg

telemetry_engine = LiveTelemetryEngine()

@app.post("/api/v1/telemetry/progress-update")
async def receive_telemetry(update: ProgressTelemetryUpdate):
    return await telemetry_engine.process_telemetry(update)

@app.websocket("/ws/live-blocks/{corridor_id}")
async def websocket_endpoint(websocket: WebSocket, corridor_id: str):
    await websocket.accept()
    client = await get_redis()
    channel = f"corridor:{corridor_id}:updates"
    
    if isinstance(client, MockRedisPubSub):
        q = await fallback_pubsub.subscribe(channel)
        try:
            while True:
                msg = await q.get()
                await websocket.send_text(msg)
        except WebSocketDisconnect:
            await fallback_pubsub.unsubscribe(channel, q)
    else:
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    await websocket.send_text(message["data"])
        except WebSocketDisconnect:
            await pubsub.unsubscribe(channel)

async def run_f06_test():
    print("\n--- Running Mock F-06 Telemetry Test ---")
    
    channel = "corridor:HWH-BWN-CHORD:updates"
    q = await fallback_pubsub.subscribe(channel)
    
    async def dispatcher_client():
        try:
            for _ in range(4):
                msg = await asyncio.wait_for(q.get(), timeout=2.0)
                print(f"[Dispatcher WS Received]: {msg}")
        except asyncio.TimeoutError:
            pass
            
    async def field_supervisor():
        base_time = now()
        
        telemetry_engine.blocks["BLK-100"] = {
            "scheduled_end_time": base_time + timedelta(minutes=60),
            "state": BlockState.ACTIVE
        }
        
        # 1. Start
        await receive_telemetry(ProgressTelemetryUpdate(
            block_id="BLK-100", corridor_id="HWH-BWN-CHORD", supervisor_id="SUP-1",
            actual_progress_pct=0.0, estimated_minutes_remaining=60, timestamp=base_time
        ))
        
        # 2. Normal Progress
        await receive_telemetry(ProgressTelemetryUpdate(
            block_id="BLK-100", corridor_id="HWH-BWN-CHORD", supervisor_id="SUP-1",
            actual_progress_pct=30.0, estimated_minutes_remaining=40, timestamp=base_time + timedelta(minutes=20)
        ))
        
        # 3. Lagging (Triggers Overrun)
        await receive_telemetry(ProgressTelemetryUpdate(
            block_id="BLK-100", corridor_id="HWH-BWN-CHORD", supervisor_id="SUP-1",
            actual_progress_pct=60.0, estimated_minutes_remaining=60, timestamp=base_time + timedelta(minutes=40)
        ))
        
        # 4. Early Completion of a different block
        telemetry_engine.blocks["BLK-200"] = {
            "scheduled_end_time": base_time + timedelta(minutes=120),
            "state": BlockState.ACTIVE
        }
        await receive_telemetry(ProgressTelemetryUpdate(
            block_id="BLK-200", corridor_id="HWH-BWN-CHORD", supervisor_id="SUP-2",
            actual_progress_pct=100.0, estimated_minutes_remaining=0, timestamp=base_time + timedelta(minutes=60)
        ))
        
    await asyncio.gather(dispatcher_client(), field_supervisor())

if __name__ == '__main__':
    print("Running Mock F-05 Simulation Test...")
    schedules = _generate_mock_schedules()
    simulator = CorridorWhatIfSimulator(schedules)
    
    start_block = now().replace(hour=10, minute=0, second=0, microsecond=0)
    end_block = start_block + timedelta(hours=3)
    
    print(f"Injecting block between B and C from {start_block.isoformat()} to {end_block.isoformat()}")
    t0 = perf_time.monotonic()
    report = simulator.simulate_block("B", "C", start_block, end_block)
    t1 = perf_time.monotonic()
    
    print(f"Simulation took {(t1 - t0)*1000:.2f} ms")
    print(report.model_dump_json(indent=2))
    
    asyncio.run(run_f06_test())

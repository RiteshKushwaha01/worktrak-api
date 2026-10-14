"""
Employee Attendance & Analytics API

Run from repo root:
    uvicorn app.main:app --port 8000

Needs MONGO_URI and MONGO_DB in the environment (.env is only for local).
"""
import calendar
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from bson.json_util import dumps, loads
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# env vars win over .env (load_dotenv does not override by default)
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set")

client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# indexes (created on startup, idempotent)
# ---------------------------------------------------------------------------

def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@app.on_event("startup")
def startup():
    create_indexes()


# ---------------------------------------------------------------------------
# helpers — time conversion, R1-R5
# ---------------------------------------------------------------------------

def round_half_up(value, places=2):
    step = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(step, rounding=ROUND_HALF_UP))


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def truncate_seconds(dt: datetime) -> datetime:
    return as_utc(dt).replace(microsecond=0)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def from_millis(ms: int) -> datetime:
    # API sends epoch ms; store UTC datetimes truncated to whole seconds (R1)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def to_millis(dt) -> Optional[int]:
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    dt = truncate_seconds(dt)
    return int(dt.timestamp() * 1000)


def as_ist(dt: datetime) -> datetime:
    return as_utc(dt).astimezone(IST)


def month_bounds(month: str):
    year, month_no = map(int, month.split("-"))
    start = date(year, month_no, 1)
    end = date(year, month_no, calendar.monthrange(year, month_no)[1])
    return start, end


def attendance_date_of(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    # R1: date is IST calendar date of punch-in, except overnight shifts
    local = as_ist(truncate_seconds(punch_in))
    day = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_at(attendance_date: str, shift_start: str) -> datetime:
    h, m = map(int, shift_start.split(":"))
    return datetime.fromisoformat(attendance_date).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )


def shift_end_at(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(attendance_date).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )
    if shift_end <= shift_start:
        end += timedelta(days=1)
    return end


def compute_late_minutes(punch_in, shift_start, shift_end, attendance_date) -> int:
    local = as_ist(truncate_seconds(punch_in))
    start = shift_start_at(attendance_date, shift_start)
    seconds_late = (local - start).total_seconds()
    if seconds_late > 10 * 60:
        return int(seconds_late // 60)
    return 0


def compute_work_hours(punch_in, punch_out) -> float:
    seconds = (truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out, shift_start, shift_end, attendance_date) -> int:
    local = as_ist(truncate_seconds(punch_out))
    end = shift_end_at(attendance_date, shift_start, shift_end)
    minutes = int((local - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def derived_fields(status, punch_in, punch_out, emp, attendance_date):
    if status in ("ABSENT", "LEAVE") or punch_in is None:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    late = compute_late_minutes(punch_in, emp["shift_start"], emp["shift_end"], attendance_date)
    if punch_out is None:
        return {
            "work_hours": None,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
        }
    hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], attendance_date)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": hours < 4.50,
    }


def serialize_employee(doc):
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_millis(doc.get("created_at")),
    }


def serialize_history(history):
    items = []
    for entry in history or []:
        changes = {}
        for field, change in (entry.get("changes") or {}).items():
            old, new = change.get("from"), change.get("to")
            if field in ("punch_in", "punch_out"):
                old, new = to_millis(old), to_millis(new)
            changes[field] = {"from": old, "to": new}
        items.append({
            "at": to_millis(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    return items


def serialize_attendance(doc):
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_millis(doc.get("punch_in")),
        "punch_out": to_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": serialize_history(doc.get("history") or []),
    }


def page_args(page: int, page_size: int):
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(status_code=422, detail="page_size must be between 1 and 100")
    return (page - 1) * page_size, page_size


def weekday_count(start: date, end: date) -> int:
    if start > end:
        return 0
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "emp_code": "EMP0099",
                "name": "Test User",
                "email": "test.user@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-07-01",
            }
        }
    )
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, value):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must be different")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "emp_code": "EMP0001",
                "punched_at": 1784088120000,
                "status": "PRESENT",
            }
        }
    )
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "emp_code": "EMP0001",
                "punched_at": 1784121000000,
            }
        }
    )
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "WFH",
                "reason": "Manager corrected status",
                "regularized_by": "MGR001",
            }
        }
    )
    status: Optional[str] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# aggregation builders (used by analytics + /admin/explain)
# ---------------------------------------------------------------------------

def employee_monthly_pipeline(emp_code: str, month: str):
    start, end = month_bounds(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
            }
        },
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]},
                                ]
                            },
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]):
    start, end = month_bounds(month)
    month_start = start.isoformat()
    month_end = end.isoformat()
    match_emps = {"joined_on": {"$lte": month_end}}
    if department:
        match_emps["department"] = department
    return [
        {"$match": match_emps},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$code"]},
                            "date": {"$gte": month_start, "$lte": month_end},
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date",
                                        "format": "%Y-%m-%d",
                                    }
                                }
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
                                    {"$gte": ["$logs.day_of_week", 2]},
                                    {"$lte": ["$logs.day_of_week", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]):
    start, end = month_bounds(month)
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def department_trend_pipeline(department: str, from_date: str, to_date: str):
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    n_days = (end - start).days + 1
    return [
        {"$match": {"department": department}},
        {
            "$facet": {
                "emps": [{"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}}],
                "daily": [
                    {
                        "$lookup": {
                            "from": "attendance_logs",
                            "let": {"code": "$emp_code"},
                            "pipeline": [
                                {
                                    "$match": {
                                        "$expr": {"$eq": ["$emp_code", "$$code"]},
                                        "date": {"$gte": from_date, "$lte": to_date},
                                    }
                                }
                            ],
                            "as": "logs",
                        }
                    },
                    {"$unwind": "$logs"},
                    {
                        "$group": {
                            "_id": "$logs.date",
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
                                        {
                                            "$cond": [
                                                {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
            }
        },
        {
            "$project": {
                "emps": "$emps",
                "daily": "$daily",
                "offsets": {"$range": [0, n_days]},
            }
        },
        {"$unwind": "$offsets"},
        {
            "$addFields": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": {
                            "$dateAdd": {
                                "startDate": {
                                    "$dateFromString": {
                                        "dateString": from_date,
                                        "format": "%Y-%m-%d",
                                    }
                                },
                                "unit": "day",
                                "amount": "$offsets",
                            }
                        },
                    }
                }
            }
        },
        {
            "$addFields": {
                "stats": {
                    "$first": {
                        "$filter": {
                            "input": "$daily",
                            "as": "row",
                            "cond": {"$eq": ["$$row._id", "$date"]},
                        }
                    }
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
                "is_working_day": {
                    "$lte": [
                        {
                            "$isoDayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$date",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        },
                        5,
                    ]
                },
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$stats.present_count", 0]},
                "late_count": {"$ifNull": ["$stats.late_count", 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


def attendance_list_filter(emp_code, date_from, date_to, status):
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


# ---------------------------------------------------------------------------
# Part A — starter endpoints (reviewed / fixed)
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip, limit = page_args(page, page_size)
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(limit)
    )
    return {
        "items": [serialize_employee(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in PRESENT_STATUSES:
        raise HTTPException(status_code=422, detail="status must be PRESENT, WFH or ON_DUTY")

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = from_millis(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date_of(ts, emp["shift_start"], emp["shift_end"])
    derived = derived_fields(body.status, ts, None, emp, day)

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": derived["work_hours"],
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_list_filter(emp_code, date_from, date_to, status)
    skip, limit = page_args(page, page_size)
    total = db.attendance_logs.count_documents(q)
    page_docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", 1)])
        .skip(skip)
        .limit(limit)
    )
    return {
        "items": [serialize_attendance(doc) for doc in page_docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Part B — remaining endpoints
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = from_millis(body.punched_at) if body.punched_at is not None else now_utc()

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    punch_in_time = truncate_seconds(record["punch_in"])
    seconds = (ts - punch_in_time).total_seconds()
    if seconds <= 0 or seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    derived = derived_fields(record["status"], punch_in_time, ts, emp, record["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Already punched out")
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid attendance date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    supplied = body.model_fields_set
    if "status" in supplied and body.status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")

    new_status = body.status if "status" in supplied else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                status_code=422,
                detail="Absent or leave records cannot have punch times",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        if "punch_in" in supplied:
            new_punch_in = from_millis(body.punch_in) if body.punch_in is not None else None
        else:
            new_punch_in = record.get("punch_in")
            if new_punch_in is not None:
                new_punch_in = truncate_seconds(new_punch_in)

        if "punch_out" in supplied:
            new_punch_out = from_millis(body.punch_out) if body.punch_out is not None else None
        else:
            new_punch_out = record.get("punch_out")
            if new_punch_out is not None:
                new_punch_out = truncate_seconds(new_punch_out)

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch-in")

        if attendance_date_of(new_punch_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(
                status_code=422,
                detail="Punch-in must be on the attendance date",
            )

        if new_punch_out is not None:
            seconds = (new_punch_out - new_punch_in).total_seconds()
            if seconds <= 0 or seconds > 24 * 60 * 60:
                raise HTTPException(status_code=422, detail="Invalid punch-out time")

    derived = derived_fields(new_status, new_punch_in, new_punch_out, emp, date)

    def add_change(changes, field, old_value, new_value, is_time=False):
        if is_time:
            old_cmp = to_millis(old_value)
            new_cmp = to_millis(new_value)
            if old_cmp != new_cmp:
                changes[field] = {"from": old_value, "to": new_value}
        elif old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    changes = {}
    add_change(changes, "status", record["status"], new_status)
    add_change(changes, "punch_in", record.get("punch_in"), new_punch_in, True)
    add_change(changes, "punch_out", record.get("punch_out"), new_punch_out, True)
    add_change(changes, "work_hours", record.get("work_hours"), derived["work_hours"])
    add_change(changes, "late_minutes", int(record.get("late_minutes") or 0), derived["late_minutes"])
    add_change(changes, "overtime_minutes", int(record.get("overtime_minutes") or 0), derived["overtime_minutes"])
    add_change(changes, "half_day", bool(record.get("half_day") or False), derived["half_day"])

    if not changes:
        raise HTTPException(status_code=422, detail="No changes supplied")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    filt = {"_id": record["_id"]}
    if "history" in record:
        filt["history"] = record["history"]
    else:
        filt["history"] = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        filt,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                **derived,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was changed by another request",
        )
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    start, end = month_bounds(month)
    result = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    stats = result[0] if result else {}

    joined_on = date.fromisoformat(employee["joined_on"])
    working_days = weekday_count(max(start, joined_on), end)
    present_days = stats.get("present_days", 0) or 0
    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(stats.get("leave_days", 0) or 0),
        "late_count": int(stats.get("late_count", 0) or 0),
        "total_late_minutes": int(stats.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    for item in items:
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
        item["present_days"] = round_half_up(item.get("present_days") or 0, 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        start_date = date.fromisoformat(from_date)
        end_date = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if end_date < start_date:
        raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")
    if (end_date - start_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    items = list(db.employees.aggregate(department_trend_pipeline(department, from_date, to_date)))
    rates = []
    for item in items:
        if item.get("attendance_rate") is not None:
            item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        item["present_count"] = round_half_up(item.get("present_count") or 0, 2)
        rates.append(item.get("attendance_rate"))
        window = [r for r in rates[-7:] if r is not None]
        item["moving_avg_7d"] = round_half_up(sum(window) / len(window), 4) if window else None
    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Part C — explain
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in allowed:
        raise HTTPException(status_code=422, detail="Unsupported endpoint name")

    if endpoint == "attendance_list":
        skip, limit_n = page_args(page, page_size)
        command = {
            "find": "attendance_logs",
            "filter": attendance_list_filter(emp_code, date_from, date_to, status),
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": limit_n,
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": employee_monthly_pipeline(emp_code, month),
            "cursor": {},
        }
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        collection = "employees"
        command = {
            "aggregate": collection,
            "pipeline": department_summary_pipeline(month, department),
            "cursor": {},
        }
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": late_leaderboard_pipeline(month, limit, department),
            "cursor": {},
        }
    else:
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        try:
            start_date = date.fromisoformat(from_date)
            end_date = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="from and to must be valid dates")
        if end_date < start_date:
            raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")
        if (end_date - start_date).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
        collection = "employees"
        command = {
            "aggregate": collection,
            "pipeline": department_trend_pipeline(department, from_date, to_date),
            "cursor": {},
        }

    explain = db.command("explain", command, verbosity="executionStats")
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": loads(dumps(explain)),
    }

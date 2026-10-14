# Employee Attendance & Analytics API

FastAPI + MongoDB backend for the HROne assignment (attendance punch in/out, regularization, analytics).

Contract is `openapi.yaml`. Data shape is `DATA_MODEL.md`. Rules R1–R10 are in both.

## Run

Python 3.11+ and MongoDB 6+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in the environment. A local `.env` works for development; real env vars override it. Do not commit `.env`.

```powershell
uvicorn app.main:app --port 8000
```

`GET /health` should return 200 once Mongo is reachable. Indexes are created on startup.

## Layout

All application code is in `app/main.py` (as required).

- Part A: review fixes — health, employees, punch-in, list attendance
- Part B: punch-out, PATCH regularize, analytics
- Part C: `GET /admin/explain/{endpoint}` and this `DECISIONS.md`

## Notes

- Identity is `emp_code`. `_id` is never returned.
- Timestamps in the API are epoch milliseconds; Mongo stores UTC datetimes.
- Attendance date is IST (overnight shifts included).
- Analytics use aggregations and the stored derived fields.

`REVIEW.md` is the starter review. If something is still off vs the hidden dataset, it will most likely be an edge case in R1–R5 or a tie/headcount detail in analytics.

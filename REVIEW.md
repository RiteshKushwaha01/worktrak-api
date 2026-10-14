# REVIEW.md

Notes from reviewing the starter `app/main.py`. Happy path mostly worked; the issues below showed up on the contract, overnight shifts, or concurrent requests.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Mongo connection | `MONGO_URI` / `MONGO_DB` had localhost defaults. Grader injects its own values and says not to hard-code this. | Start the app with different env vars and see it still talk to localhost. | Read both from the environment after `load_dotenv()` (env wins). Fail on startup if either is missing. |
| 2 | `GET /health` | Always returned 200. Spec says 200 only if Mongo answers. | Stop Mongo and hit `/health`. | `db.command("ping")`. If that fails, 503. |
| 3 | startup / indexes | Seeded DB only has `_id`. No unique emp_code or (emp_code, date). List/analytics would collscan on ~100k logs. | Inspect indexes after start. Send two punch-ins for the same day at once. | Create indexes on startup: unique `emp_code`, unique `(emp_code, date)`, list sort index, plus department/joined_on and date/late_minutes. |
| 4 | `POST /employees` | Check-then-insert. Two concurrent creates with the same code can both pass the find. `created_at` was naive `datetime.now()`. No shift_start != shift_end check. | Duplicate `emp_code` in parallel. Look at `created_at` in the response (must be epoch ms). | Unique index + `DuplicateKeyError` → 409. Store UTC datetime, return epoch ms. Validate the body against the OpenAPI fields. |
| 5 | `GET /employees` | Not sorted by `emp_code`. `page` / `page_size` not bounded. `created_at` left as a datetime. `_id` could leak if the projection was wrong. | Filter a department, check `total`, request page 2, look at field types. | Sort by `emp_code`, R10 pagination, serialize employees without `_id`. |
| 6 | timestamp helpers | API is epoch milliseconds. Responses were returning BSON datetimes (JSON would show ISO strings). Seconds-looking values were not rejected. Instants were not truncated to whole seconds. | POST punch-in with ms timestamp and inspect `punch_in`. Send `1783312500` (seconds). | Convert at the boundary, reject values outside the epoch-ms range via the models, truncate to seconds before calc/store/return. |
| 7 | attendance date (R1) | Overnight handling was applied twice in late-minutes (once when picking the date, again inside the helper), which shifted the shift-start back an extra day. | Shift 22:00–06:00, punch-in 02:00 IST. Date should be the previous day; late should be vs 22:00 that day. | One function decides the attendance date. Late/overtime use that date as the shift-start day. Overnight end is +1 calendar day. |
| 8 | `compute_late_minutes` | Grace is *strictly* more than 10 minutes, and late minutes are from shift start, not from 10 past. | Shift 09:30: `09:40:00` → 0, `09:40:01` → 10, `10:15:59` → 45. | `seconds_late > 10 * 60`, then `floor` whole minutes since start. |
| 9 | `POST /attendance/punch-in` | Relied on find-then-insert. Status was not limited to PRESENT/WFH/ON_DUTY. Response included Mongo `id`. | Two simultaneous punch-ins for the same employee/date. Punch-in with status `LEAVE`. | Unique `(emp_code, date)` is the actual guard (409 on duplicate key). Validate status. Serialize without `_id`. |
| 10 | `GET /attendance` | No `date_from > date_to` → 422. Sort was only by date. Pagination not capped. Datetimes not converted. | `date_from` after `date_to`. Two people on the same date — order should be `emp_code` asc after date desc. | Same filter for count and find. Sort `(date desc, emp_code asc)`. Page bounds. Serialize records. |
| 11 | punch-out | Check-then-update can let two punch-outs both succeed. Derived fields were inlined instead of using the same helpers. Response had `id`. | Concurrent punch-outs. Overnight punch-in, punch-out the next morning. | `find_one_and_update` only if `punch_out` is still null. Reuse R3–R5 helpers. Serialize. |
| 12 | work hours / OT / half day | Float rounding and ignoring the 30 min OT floor give the wrong stored values. Analytics read whatever we stored. | Duration that needs half-up (not banker's). OT 29 vs 30 min. Rounded hours 4.49 vs 4.50. | `Decimal` half-up for hours. OT floor minutes, 0 if < 30. `half_day` from the rounded hours. |
| 13 | attendance JSON | Spec: `_id` never in a response. Record key is `(emp_code, date)`. History times and nested punch changes must be epoch ms, but Mongo stores BSON dates. | Look at punch-in / PATCH / list payloads. | `serialize_attendance` / `serialize_history` convert at the edge. |
| 14 | PATCH history | Replacing `history` loses earlier corrections. Concurrent PATCH can drop an entry. `at` was stored as an int. Punch `from`/`to` were stored as ms instead of datetimes. | Correct the same day twice. Fire two PATCH requests together. | Append one entry (`$push`). Optimistic match on current `history` → 409 if it changed. Store datetimes in Mongo, convert on the way out. |

## Looked at, left as-is

- Attendance is keyed by `(emp_code, date)`. No public record id.
- `work_hours`, `late_minutes`, `overtime_minutes`, `half_day` are stored. Analytics read those, they don't recompute from punch times.
- A missing weekday is not `ABSENT`.
- Punch-in / punch-out do not write `history`.
- Dates/months stay `YYYY-MM-DD` / `YYYY-MM` strings. Instants are UTC datetimes in Mongo.

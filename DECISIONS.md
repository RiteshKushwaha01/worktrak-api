# DECISIONS.md

1. **Indexes.** Unique `emp_code` on employees, unique `(emp_code, date)` on logs (punch-in lock + lookup), `(date desc, emp_code asc)` for the list sort, `(department, joined_on)` for headcount, and `(date, late_minutes)` for the late board. I skipped extra single-field indexes unless explain showed a collscan.

2. **Punch-in race.** Both requests can miss the existing row. The unique pair lets only one insert succeed (201). The other gets `DuplicateKeyError` → 409. The pre-check is just a faster fail.

3. **Ties.** `$rank` on `total_late_minutes` desc (1, 2, 2, 4). Keep `rank <= limit`, so a tie at the cutoff all appear — more rows than `limit` is expected. Then sort minutes desc, `emp_code` asc. Rank is not the array index.

4. **Headcount.** Start from employees with `joined_on` on or before month end, `$lookup` logs, unwind with `preserveNullAndEmptyArrays` so people with no logs still count. Later joiners never match.

5. **100x.** Check explain and latency on the real filters first, then tighten the date/emp_code/late indexes. Keep derived fields stored so analytics don't redo R2–R5.

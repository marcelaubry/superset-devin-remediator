# Concurrency and spend controls

Every step that spends ACUs or must be exclusive takes a **capacity lease**
first. Leases are rows in `capacity_leases` (migration `0006`), so they are
shared by every worker process and survive restarts; nothing is held in
process memory.

```text
Settings                         capacity_leases (PostgreSQL)
MAX_CONCURRENT_TRIAGE=2          kind=TRIAGE       scope=""            one row per held slot
MAX_CONCURRENT_REMEDIATION=1     kind=REMEDIATION  scope=""            ┐ both rows taken,
MAX_CONCURRENT_REMEDIATION_      kind=REMEDIATION  scope=owner/repo    ┘ per-repo limit checked
  PER_REPOSITORY=1
MAX_CONCURRENT_PROBES=1          kind=PROBE        scope=""
(manifest resource_keys)         kind=RESOURCE     scope=<key>         limit 1 per key
```

## Acquisition

`CapacityManager.acquire()` runs inside the caller's transaction:

1. `pg_advisory_xact_lock(kind)` — one acquirer per kind at a time across
   all workers, released with the transaction.
2. `reconcile_finished()` — leases whose attempt is no longer active are
   released (safety net for a crash between settling and releasing).
3. If this case already holds a live lease of that kind/scope it is renewed
   and returned (idempotent; a crash between acquire and commit cannot
   double-count). A lapsed own lease is released and the case competes again.
4. `count(active) >= limit` → `CapacityDenied`; otherwise a row is inserted
   with `expires_at = now + CAPACITY_LEASE_GRACE_SECONDS`.

A lease is *active* while `released_at IS NULL AND expires_at > now()`. The
partial unique index `(case_id, kind, scope) WHERE released_at IS NULL`
guarantees one live lease per case per kind, which together with the
partial unique index on unfinished attempts gives **one active remediation
per case**.

## Order of operations for a remediation

```text
approval confirmed, probe snapshotted
→ PROBE lease → base probe must FAIL as declared        (release PROBE)
→ RESOURCE leases for every manifest resource_key      (queue here on conflict)
→ REMEDIATION lease (global) + REMEDIATION lease (repo scope)
→ POST /sessions                                        ← first ACU spent
→ ... session polling, PR discovery ...
→ PROBE lease → head probe must PASS                    (release PROBE)
→ REMEDIATION leases released when the attempt settles
```

Denial at any point before `POST /sessions` costs nothing: no attempt row,
no HTTP request. The case keeps its state, `cases.waiting_for` names the
limit (`TRIAGE (2/2)`, `REMEDIATION:owner/repo (1/1)`, `PROBE (1/1)`,
`RESOURCE:lockfile:superset-frontend (1/1)`), `waiting_since` is set, and the worker re-claims it after
`CAPACITY_WAIT_BACKOFF_SECONDS`. Denials increment
`capacity_denied_total{kind}`; parked cases show up in
`cases_waiting_for_capacity`.

## Liveness and recovery

- **Heartbeat.** While a job runs, the worker loop extends `expires_at` for
  every lease it holds (`CapacityManager.heartbeat`). A worker that dies
  stops heartbeating and its leases lapse after `CAPACITY_LEASE_GRACE_SECONDS`
  (default 600 s); `expire_stale()` marks them released and counts them in
  `reconciliations_total{kind="capacity_lease",result="expired"}`.
- **Duplicate workers.** Two workers racing for the last slot serialize on
  the advisory lock; the loser sees the winner's committed row. Tested with
  two `CapacityManager` owners in `tests/integration/test_capacity.py`.
- **Cancellation.** Cancelling a running remediation moves it to
  `REMEDIATION_TERMINATION_PENDING`; the REMEDIATION leases are kept until
  Devin confirms termination, because a session that is still running is
  still spending and still occupies the organisation's concurrency.
- **Raising a limit** takes effect on the next acquire; lowering it never
  evicts a holder, it only stops new admissions.

## Proven by tests

`tests/integration/test_capacity.py` (real PostgreSQL):

- twenty eligible cases queue at `MAX_CONCURRENT_TRIAGE`, all eventually
  settle, at most one triage attempt each;
- raising the limit admits exactly that many more;
- expired leases recover without operator action;
- two workers cannot exceed the limit;
- waiting spends zero ACUs (fake Devin create count unchanged);
- cancel keeps the lease until termination is confirmed;
- conflicting resource keys serialize while distinct keys run together;
- the per-repository limit applies independently of the global one;
- waiting cases are admitted FIFO: a freed global slot is refused to a newer
  case while an older live waiter for the same limit exists
  (`CapacityDenied.queued_behind`), and the worker claims waiting cases by
  `waiting_since` ascending.

`scripts/simulate.py --scenario concurrency-20` repeats the twenty-case
queueing against the running compose stack and reads `capacity_limit`,
`active_jobs` and `cases_waiting_for_capacity` from `/metrics`.

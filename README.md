# RailSync — Automatic Block Planning

## Purpose and scope

RailSync is a decision-support prototype for the SIH Automatic Block Planning problem. It coordinates fixed-infrastructure maintenance from Engineering, Signal & Telecommunication (S&T), and Traction Distribution, then produces a proposed weekly or monthly block plan.

It never sanctions, activates, extends, or restores a railway block. A controller remains responsible for every operational decision. The governing scope is [FEATURE_SPECIFICATIONS.md](FEATURE_SPECIFICATIONS.md).

This is the handoff document for F-01 to F-08 work. It focuses on the tested **manual workflow**, because authorised live source-system integrations are outside this prototype.

## Non-negotiable rules

- Never seed, invent, infer, or silently substitute railway maintenance, COA, timetable, goods, or safety data.
- Retain source identifiers, source payloads, timestamps, policy/rule versions, inputs, and outputs.
- Return **Needs review** when data is missing, ambiguous, invalid, or unavailable.
- Open-Meteo is advisory input only. The user must supply the applicable railway-approved safety limits.
- F-03 is an explainable weighted-policy baseline, not an ML prediction. The supplied joblib model is not used because it is a rolling-stock model without the required fixed-infrastructure/corridor fields.
- Every F-04 output is a **PROPOSED** plan. Controller sanction remains mandatory.
- Every F-07 cockpit metric and timeline slot is grounded strictly in stored database records; mock data is never substituted.
- Every F-08 decision requires a justification and is written to an append-only, hash-chained ledger. The database additionally rejects audit-row updates and deletions.
- F-08 enforces the supplied controller/reviewer role at the API boundary; integrating those asserted roles with the railway identity provider remains a deployment requirement.

## Completed scope

| Feature | Status | Main page | Outcome |
| --- | --- | --- | --- |
| F-01 | Complete | /intake | Controlled mappings plus TMS/SMMS/TDMS task normalization. |
| F-02 | Complete | /feasibility | Candidate-window weather and rule assessment. |
| F-03 | Complete | /priority | Explainable score, tier, and factor contributions. |
| F-04 | Complete | /planner | Weekly/monthly proposed plan and deferred reasons. |
| F-05 | Complete | /what-if | What-if operational-impact simulation. |
| F-06 | Complete | /live-monitor | Live execution monitoring and alerts. |
| F-07 | Implemented | /cockpit | Database-grounded operations cockpit, master corridor timeline, KPIs, and multi-department block inspection. Projected availability stays unavailable until an approved baseline is imported. |
| F-08 | Implemented | /cockpit | Role-guarded sanction/override authority, append-only hash-chained audit ledger, and Combined Block Sanction Memo draft export (PDF/HTML). |

## Start locally

~~~powershell
python -m pip install -r requirements.txt
$env:RAILSYNC_ACTOR = "Ananda Jana"
$env:RAILSYNC_ROLE = "Section Controller"
python -m uvicorn backend.main:app --reload
~~~

Open http://127.0.0.1:8000/. Restart after backend edits. The submitted test records are in rail_sync.db; do not delete it if they are needed.

F-08 decisions use the server-configured `RAILSYNC_ACTOR` and `RAILSYNC_ROLE`. The cockpit reads and locks that identity before a decision can be submitted, preventing a browser/server mismatch. Supported roles are `Section Controller`, `Chief Controller`, and `Safety Officer`. In a real deployment, replace these local environment settings with the railway identity provider integration.

The F-07/F-08 operations cockpit uses a light, high-contrast theme. Department and decision-state colors remain reserved for their operational meanings, while stored database values continue to be the only source for metrics, timeline blocks, and audit details.

## Primary manual F-01 → F-08 workflow

~~~text
Verified controlled mapping + source maintenance ticket
                    ↓
F-01 normalized task
                    ↓
F-02 feasibility for a proposed COA hour
                    ↓
F-03 transparent priority score
                    ↓
F-04 considers every eligible task automatically
                    ↓
Manual COA JSON → proposed coordinated plan
                    ↓
F-05 what-if simulation to assess impact
                    ↓
F-06 live monitoring and alerts
                    ↓
F-07 dispatcher operations cockpit (Gantt timeline & KPIs)
                    ↓
F-08 human sanction / override / rejection & audit logging
                    ↓
Combined Block Sanction Memo draft (PDF / Print Draft; authority template approval required)
~~~


F-04 has no manual task selector by design. It considers all complete F-01 tasks and reports why each is eligible or not ready.

### Step 1 — F-01: register a controlled network reference

Open **Data intake** and use **Register reference** before ingesting a ticket. This is the authoritative mapping between a source-system asset reference and a controlled railway section.

The first tested TMS mapping was:

| Field | Value |
| --- | --- |
| Source system | TMS |
| Source reference | TRK-HWH-BDC-01 |
| Controlled section ID | HWH-BDC-UP-MAIN |
| Asset type | TRACK |
| Start kilometre | 40 |
| End kilometre | 50 |
| Asset reference | HWH-BDC Up Main Track |

The application rejects an end kilometre before the start kilometre and duplicate source-system/reference mappings. It never guesses a controlled mapping from a partial asset name, station name, mast, or kilometre range.

### Step 2 — F-01: ingest a maintenance ticket

Switch to **Ingest maintenance record**. Select the source system, paste the unaltered source JSON object, then enter the planning context needed for scheduling: severity, due time, duration, crews, equipment, isolation requirements, and co-working compatibility.

The first tested TMS source record was:

~~~json
{
  "ticket_id": "TMS-TEST-001",
  "track_id": "TRK-HWH-BDC-01",
  "km_start": 42.7,
  "km_end": 42.9,
  "defect_class": "WELD_DEFECT",
  "date_detected": "2026-09-06T09:30:00+05:30",
  "speed_restriction_applied": true
}
~~~

The manually entered planning context was:

| Field | Value |
| --- | --- |
| Source system | TMS |
| Severity | HIGH |
| Estimated duration | 90 minutes |
| Due date/time | 2026-09-08 02:00 |
| Required crews | P-Way crew and Safety supervisor, one per line |
| Required equipment | Ultrasonic flaw detector and Hand tools, one per line |
| Requires traffic block | Checked |
| Requires traction disconnection | Not checked |
| Compatible with co-working | Checked |

Expected outcome: **Normalized task ready**, with ID TMS-TEST-001, department ENGINEERING, section HWH-BDC-UP-MAIN, and range KM 42.7–42.9.

The same process was completed for SMMS-TEST-002, an S&T task on the same controlled section with a 60-minute duration. This shared controlled-section identity is what allows F-04 to coordinate the two departments.

#### Required source fields

| Source | Required fields |
| --- | --- |
| TMS | ticket_id, track_id, km_start, km_end, defect_class, date_detected |
| SMMS | fault_id, station_code, gear_type, failure_category, reported_ts, urgency_code |
| TDMS | defect_no, ohe_substation, mast_from, mast_to, issue_type, scheduled_date |

F-01 behaviour:

- **COMPLETE**: valid source fields and a registered controlled mapping. The task can continue to F-02.
- **NEEDS_REVIEW**: source data is retained, but no section/KM information is inferred. For example, an SMMS station code with no registered controlled reference cannot be planned.
- Duplicate source ticket: a complete record cannot be normalized twice.

### Step 3 — F-02: assess a candidate time

Open **Feasibility**, select a normalized task, enter verified section coordinates, a proposed block time, and applicable authority-approved rules.

For both tested tasks, the proposed time was 2026-09-07T02:00:00Z. This matters because F-04 only accepts a COA window that begins in the same UTC hour as the eligible F-02 assessment.

F-02 gets the actual hourly Open-Meteo forecast for the selected coordinates and hour. The planner must explicitly enter:

- Rule version/circular reference;
- Engineering maximum temperature for Engineering work;
- Traction maximum wind speed for Traction work;
- Traffic-block minimum visibility where a traffic block is required;
- Caution risk multiplier where a traffic block is required.

| Result | Effect on later planning |
| --- | --- |
| SUITABLE | Eligible for F-03 and F-04. |
| CAUTION_REQUIRED | Eligible; the caution/risk remains visible. |
| NOT_SUITABLE | Not eligible for F-04. |
| NEEDS_REVIEW | Not eligible; forecast or an applicable approved limit is unavailable. |

There is no weather fallback. “No forecast available” is a correct safety outcome, not a value to replace with invented weather.

### Step 4 — F-03: calculate priority manually

Open **Priority** and select a task with a latest F-02 result of SUITABLE or CAUTION_REQUIRED.

Enter real operational context:

- Passenger trains per day;
- Goods forecast per day;
- Section traffic in GMT;
- Route criticality from 0 to 100;
- Confirmed active operational restriction, if any.

Then enter the approved policy/circular version, severity scores, normalisation maxima, factor weights, and tier thresholds. Values are deliberately not hardcoded.

The tested run produced:

| Task | Department | F-03 score |
| --- | --- | --- |
| SMMS-TEST-002 | SIGNAL_TELECOM | 55.42 |
| TMS-TEST-001 | ENGINEERING | 53.17 |

The result panel gives a 0–100 score, tier, and individual weighted factor contributions. This is a policy calculation, not a synthetic ML prediction. The backend supports reusable approved-policy registration, but the manual form remains the tested UI path.

### Step 5 — F-04: inspect the automatic task pool

Open **Block plan**. The **Tasks considered by the optimizer** table lists all complete F-01 tasks automatically. A task becomes eligible only when:

1. F-01 status is COMPLETE.
2. Latest F-02 result is SUITABLE or CAUTION_REQUIRED.
3. A latest F-03 priority evaluation exists.

The table shows task ID, department, section, duration, priority score, and the required COA start hour. Do not add a manual task selection step in future work: automatic cross-department consideration is central to the SIH requirement.

### Step 6 — F-04: paste a COA window and generate a plan

The tested manual horizon was:

| Field | Value |
| --- | --- |
| Planning horizon | Weekly |
| Horizon start | 2026-09-06 00:00 |
| Horizon end | 2026-09-13 00:00 |

The following was the labelled demonstration COA window:

~~~json
[
  {
    "corridor_id": "DEMO-COA-001",
    "section_id": "HWH-BDC-UP-MAIN",
    "start_time": "2026-09-07T02:00:00Z",
    "end_time": "2026-09-07T03:30:00Z",
    "max_simultaneous_crews": 4,
    "traffic_block_available": true,
    "traction_disconnection_available": false,
    "timetable_reference": "DEMO-TT-2026-09-07",
    "goods_forecast_reference": "DEMO-GF-2026-09-07",
    "passenger_trains_affected": 0,
    "goods_trains_affected": 0
  }
]
~~~

Capacity is 4 because each test task declares two crew entries. Capacity 2 is insufficient for simultaneous work, so F-04 correctly sequences them.

Expected plan:

- One shared 90-minute corridor block;
- SMMS-TEST-002 from 02:00 to 03:00 UTC;
- TMS-TEST-001 from 02:00 to 03:30 UTC;
- Block use of 90 / 90 minutes;
- Two departmental tasks completed in one corridor disruption rather than two separate blocks.

#### Parallel-work rule

F-04 permits parallel work only when all concurrent tasks:

1. Explicitly declare co-working compatibility in F-01;
2. Fit within COA crew capacity;
3. Have no overlapping declared crew names;
4. Have no overlapping declared equipment names; and
5. Have all required traffic-block and traction-disconnection availability.

Otherwise tasks are sequenced. This is a conservative software rule, not a claim that all real railway work must always be sequential.

#### F-04 hard checks

Before placement, the optimizer checks matching controlled section, F-02 assessed hour, due date, duration, traffic-block/disconnection availability, declared crew capacity, and crew/equipment conflicts. A task that cannot be placed is shown in **Planner review queue** with a reason; it is never silently omitted.

## Optional imported-data workflow

The Operations data page is an additive F-01/F-04 route, not a replacement for the manual path. Use it only when verified operational exports are available:

1. Import timetable occupancy.
2. Import goods forecast.
3. Import COA availability that cites both records.

The application rejects a COA window if cited records are absent, in another section, or fail to cover the window interval. “Generate from stored COA data” then invokes F-04 from saved source records. The manual COA JSON workflow above remains the recommended demonstration flow.

## API and database handoff

The backend is [backend/main.py](backend/main.py), implemented with FastAPI and SQLite in rail_sync.db.

| Function | Endpoint | Table |
| --- | --- | --- |
| F-01 controlled mapping | POST /api/v1/network-references | network_references |
| F-01 task ingest/list | POST/GET /api/v1/ingestion/tasks | ingestion_records |
| F-02 | POST /api/v1/feasibility/assessments | feasibility_assessments |
| F-03 | POST /api/v1/priority/evaluations | priority_evaluations |
| Reusable F-03 policies | GET/POST /api/v1/priority/policies | priority_policies |
| F-04 readiness | GET /api/v1/block-plans/eligibility | — |
| F-04 manual plan | POST /api/v1/block-plans | block_plans |
| Optional timetable import | POST /api/v1/operations/timetable-occupancy | timetable_occupancy |
| Optional goods import | POST /api/v1/operations/goods-forecasts | goods_forecasts |
| Optional COA import | POST /api/v1/operations/coa-windows | coa_windows |
| F-04 stored-data plan | POST /api/v1/block-plans/from-integrated-data | block_plans |

## Files future contributors need

| File | Responsibility |
| --- | --- |
| [backend/main.py](backend/main.py) | Models, validation, persistence, weather lookup, priority scoring, optimizer. |
| [intake.html](intake.html), [intake.js](intake.js) | F-01 manual mapping and ticket entry. |
| [feasibility.html](feasibility.html), [feasibility.js](feasibility.js) | F-02 manual assessment. |
| [priority.html](priority.html), [priority.js](priority.js) | F-03 manual evaluation. |
| [planner.html](planner.html), [planner.js](planner.js) | F-04 manual COA planner and plan rendering. |
| [what-if.html](what-if.html), [what-if.js](what-if.js) | F-05 what-if operational impact simulation. |
| [live-monitor.html](live-monitor.html), [live-monitor.js](live-monitor.js) | F-06 live execution monitoring. |
| [FEATURE_SPECIFICATIONS.md](FEATURE_SPECIFICATIONS.md) | Full F-01 through F-08 scope. |

## F-07/F-08 implementation notes

Refer to `FEATURE_SPECIFICATIONS.md` for the governing requirements and use these notes when extending the implemented cockpit.

### F-07: Planner and dispatcher operations cockpit
- **Objective:** Build a unified operational interface integrating F-01 to F-06 features.
- The cockpit overlay is closed by default and is opened only after selecting a stored block; its controls remain available for normal input.
- **Implementation Strategy:**
  - Create a new frontend dashboard (e.g., `cockpit.html`).
  - Extend the corridor timeline with imported COA availability when its source record is present; never synthesize an availability window.
  - Provide a consolidated view of tasks with their priority, data-quality warnings, and feasibility states.
  - Keep every KPI tied to stored records. Do not calculate projected availability until an authority-approved capacity baseline is available.
  - **Design Note:** Strictly use high-contrast visual encoding to distinguish between *proposed*, *sanctioned*, and *live* states.

### F-08: Human approval, overrides, audit trail and formal reporting
- **Objective:** Implement role-based access control, an append-only audit trail, and plan reporting/export.
- **Implementation Strategy:**
  - Integrate the API's role guard with the railway identity provider before deployment; the prototype validates supplied role names but does not authenticate an identity.
  - Add database tables for an append-only audit trail, tracking every recommendation, approval, override, actor, and timestamp.
  - Enforce logic where deviations from recommended plans require explicit reason codes.
  - Implement an export feature (PDF or structured template) for a sanction-memo draft. Do not use unapproved official templates.

## Boundary for F-07+

Future work must consume the F-01–F-06 audit trail and proposed-plan output; it must not overwrite historical source records, assessments, scores, or plans. Keep approvals and execution state separate from a proposed plan. Never turn a recommendation into live movement authority, a block extension, a signalling instruction, or a restoration command.

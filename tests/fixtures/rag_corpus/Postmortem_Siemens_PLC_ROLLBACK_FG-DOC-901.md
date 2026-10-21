# Deployment Failure Postmortem
## Siemens PLC — ROLLBACK Pattern
### Fixture · FG-DOC-901

| Field | Value |
|---|---|
| Document ID | FG-DOC-901 |
| Vendor | Siemens |
| Device Family | PLC |
| Evidence Strength | HIGH |

## Root Cause Analysis
A synthetic cluster: upgrades that touched the kernel rolled back. Cited
records DEP_000101 and DEP_000102.

## Lessons Learned
Stage kernel changes on a twin before the plant floor.

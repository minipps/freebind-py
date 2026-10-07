# Freebind Python implementation documents

Start with [PLAN.md](PLAN.md), then choose work from [TASKS.md](TASKS.md). These
documents preserve the research and decisions agreed on 2026-10-07. T01–T23
are implemented, reviewed, committed, and pushed. T24 artifact validation is
in progress; current assignments and evidence are in [TASKS.md](TASKS.md).

## Document responsibilities

- **PLAN.md:** agreed behavior, public interfaces, research sources, compatibility
  bounds, and release acceptance criteria. It is the source of truth for behavior.
- **TASKS.md:** atomic deliverables, dependencies, file ownership, progress, and
  handoffs. It is the source of truth for implementation status.

## Working with concurrent agents

1. Designate one coordinator. Only that coordinator edits the task ledger, assigns
   owners, approves interface changes, and integrates completed work.
2. Assign a task only when its dependencies are complete and integrated into the
   shared baseline. A task with overlapping write paths must wait for the current
   owner, even when its dependencies otherwise permit it to start.
3. Give each agent its task ID, baseline revision, reserved paths, and acceptance
   criteria. Agents read both documents before implementing and inspect existing
   code before adding helpers or dependencies.
4. Prefer a separate Git worktree/branch per agent once this workspace is a real
   Git repository. Worktrees do not remove the need to reserve overlapping paths.
   Without Git, use the shared workspace with one writer per reserved file.
5. Workers edit only their reserved paths. Request a reservation before changing a
   shared export, metadata, another subsystem, or an existing test owned by another
   agent. Do not overwrite, reset, or remove another agent's work.
6. Workers report results to the coordinator using the handoff template below.
   The coordinator reviews the diff, integrates it, runs relevant checks, updates
   the ledger, and releases the reservation.
7. A failed test or unavailable privilege is a reported limitation, not a passing
   check. Mark a task blocked only when the recorded external condition prevents
   completing it. Implement unrelated available tasks while it is unresolved.

Use the task ledger and the available orchestrator for assignments; no custom
scheduler, lock service, or additional coordination application is needed. A
status row alone is not a synchronization primitive: assignments must be made
serially by the coordinator before workers start.

### Handoff template

```text
Task: Txx
Owner:
Baseline revision (or shared-workspace baseline):
Branch/worktree, if applicable:
Changed paths:
Behavior delivered:
Checks run and exact results:
Remaining limitations/blockers:
Interface changes requested (or none):
Suggested follow-up tasks:
```

An agent may return a handoff in its orchestrator message; it does not need to
create another documentation file. The coordinator records the concise result
and revision in the ledger's handoff column.

## Implementation boundaries

Implement the agreed pure-Python core and optional integrations. Do not create a
CFFI backend, packet-rewriting daemon, new HTTP client, speculative abstraction,
or publication workflow. Keep the core dependency-free; reuse the selected
clients' streams, TLS, and pooling. Every nontrivial task includes its smallest
meaningful runnable check.

The read-only sandbox rejects ordinary socket construction with `EPERM`.
Outside that sandbox, the isolated namespace harness passed real IPv4/IPv6
source and return-traffic checks. The full T22 parity suite also passed locally and in hosted CI with no skips
and all client capabilities dropped.
Do not infer that Freebind itself requires elevated privileges from the sandbox
result.

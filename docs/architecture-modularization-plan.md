# Architecture modularization plan

- **Status:** Proposed
- **Created:** 2026-07-20
- **Scope:** Repository structure, module ownership, dependency direction,
  compatibility seams, frontend module loading, and architecture verification

## Purpose

Hermes WebUI has already extracted several meaningful owners from its largest
files. The remaining problem is no longer just file size. Different subsystems
still use different module shapes, compatibility mechanisms, dependency
directions, and frontend loading conventions.

This plan defines one consistent target architecture and a staged migration
toward it. It complements the current-state map in [`../ARCHITECTURE.md`](../ARCHITECTURE.md),
the dated findings in [`architecture-audit-2026-07-19.md`](architecture-audit-2026-07-19.md),
and the state and durability contracts routed through [`CONTRACTS.md`](CONTRACTS.md).
It does not authorize one large rewrite or override an accepted subsystem RFC.

## Assessment

The repository is substantially more modular than the original audit snapshot,
but it is not yet consistently modular:

- backend domains use several variants of `foo.py` plus `foo_parts/`
- compatibility facades use multiple custom binding and rebinding mechanisms
- `api/` remains a broad, mostly flat directory containing transport, domain,
  runtime, persistence, and adapter modules side by side
- route and streaming modules still coordinate domain state and therefore act
  as dependency hubs
- configuration, profile, and provider modules have unclear dependency
  direction and participate in cycles
- frontend domains use a mixture of classic globals, ordered parts, facades,
  factories, and native ES modules
- many tests know source locations and private globals rather than crossing the
  owning module's interface

Large files are evidence to investigate, not an architectural diagnosis by
themselves. A cohesive 800-1,200 line implementation can be preferable to
fragments that split functions, hide execution order, or require source
composition. Conversely, a 200-line pass-through module can be too shallow if
its callers must still understand all implementation details.

## Goals

1. Give each domain one recognizable package shape.
2. Put each mutable state transition behind one owning module.
3. Make dependency direction visible and mechanically enforceable.
4. Keep public interfaces small while allowing cohesive implementations to
   remain together.
5. Separate HTTP transport from run, session, provider, and workspace behavior.
6. Replace implicit global rebinding with direct imports and explicit adapters
   at real seams.
7. Give the vanilla JavaScript frontend one native-module convention without
   introducing a bundler or framework.
8. Make tests stable across internal file movement by testing module interfaces
   and observable behavior.
9. Preserve current behavior and compatibility throughout incremental,
   independently reviewable migrations.

## Non-goals

- enforcing a hard 500-line limit
- splitting functions or cohesive owner closures across files
- composing source fragments with `exec()`, concatenation, or load-order tricks
- adding a frontend framework, bundler, or new build step
- rewriting all backend modules in one change
- changing storage formats, recovery semantics, or user-visible behavior as a
  side effect of repository restructuring
- introducing an adapter where only one implementation exists and no behavior
  actually varies

## Architecture vocabulary

This plan uses the following terms consistently:

- **Module:** a package, class, function, or slice with one interface and an
  implementation
- **Interface:** everything callers must know, including types, invariants,
  ordering, errors, and configuration
- **Implementation:** code hidden behind a module's interface
- **Depth:** the leverage provided by an interface; a deep module hides
  substantial behavior behind a small interface
- **Seam:** the location at which an interface lives and behavior can vary
- **Adapter:** a concrete implementation used at a seam
- **Locality:** concentrating related knowledge, changes, bugs, and verification
  in one place

The deletion test applies to every proposed module: if deleting it merely moves
its complexity into many callers, the module provides useful depth. If deleting
it makes complexity disappear, it is probably a shallow pass-through.

## Target backend structure

The exact internal filenames may evolve during implementation, but the domain
grouping and dependency direction should converge on this shape:

```text
api/
  http/
    router.py
    middleware/
      auth.py
      csrf.py
      errors.py
    routes/
      sessions.py
      runs.py
      providers.py
      workspace.py
      updates.py

  runs/
    __init__.py
    admission.py
    execution.py
    runtime_state.py
    event_sink.py
    journal.py
    channels.py
    local.py
    gateway.py
    background.py

  sessions/
    __init__.py
    repository.py
    lifecycle.py
    recovery.py
    projection.py
    sources.py
    events.py
    export.py

  config/
    __init__.py
    io.py
    settings.py
    model_catalog.py
    model_cache.py

  profiles/
  providers/
  workspace/
  updates/
  extensions/
  auth/

  adapters/
    hermes_agent.py
    filesystem.py
    git.py
```

This is a responsibility map, not a demand for one file per listed name. Two
closely coupled responsibilities may share an implementation file when doing so
improves locality. A package may also use additional private modules without
making them part of its interface.

## Standard Python package pattern

Every migrated backend domain should follow the same rules:

1. Use a real package instead of `foo.py` plus `foo_parts/`.
2. Keep the existing public import path where practical, for example
   `import api.config` backed by `api/config/__init__.py`.
3. Treat `__init__.py` as the domain's small public interface, not as a second
   implementation file.
4. Use direct relative imports inside the package.
5. Do not import private modules from another domain. Cross-domain callers use
   that package's public interface.
6. Keep mutable state and its complete lifecycle under one owner.
7. Put optional or replaceable external behavior behind an adapter only when a
   real seam exists.
8. Keep temporary compatibility exports isolated and documented with a removal
   condition.
9. Do not resolve ordinary internal calls through `sys.modules`, module-global
   registries, or runtime rebinding.

Because a same-named `foo.py` and `foo/` package cannot coexist as the canonical
module, each domain conversion must be an atomic, separately verified change.

## Dependency direction

The intended backend direction is:

```text
HTTP transport
      |
      v
run and session modules
      |
      v
domain persistence and external adapters
```

Configuration-related dependencies should flow as follows:

```text
config foundation -> profiles -> providers -> run orchestration
```

The following rules should eventually be checked in CI:

- HTTP route modules may depend on domain modules; domain modules must not
  depend on the HTTP router or route facades.
- Session and run implementations must not import `api.routes`.
- Configuration foundations must not import profiles or providers.
- Provider implementations may consume a resolved profile/configuration
  snapshot; configuration foundations must not call back into providers.
- Persistence modules must not depend on HTTP or streaming transport.
- Cross-domain imports use public package interfaces, not another package's
  private implementation files.
- New cyclic package dependencies are rejected.
- Existing cycles are recorded and removed as their domains migrate.

## Transport and domain separation

HTTP modules should be intentionally shallow adapters. Their responsibilities
are limited to:

1. parse and validate the request
2. apply authentication, authorization, and CSRF rules
3. call the owning domain module
4. translate its result or error into an HTTP response

Session projection belongs to the session package. Run admission and execution
belong to the run package. Provider discovery belongs to the provider/config
packages. Workspace and Git behavior belong to the workspace package. Moving a
function out of `routes.py` is complete only when its dependencies and state
ownership move with it; placing the same orchestration under `routes_parts/`
does not establish a deeper module.

Authentication and CSRF ordering are security invariants. Their preservation
must be verified behaviorally during router migration rather than inferred from
source position alone.

## Run, turn, stream, and session domains

The repository should use these meanings consistently:

- **Run:** one active agent execution
- **Turn:** one durable user/assistant interaction within a session
- **Stream:** live and replayable events produced by a run
- **Session:** the durable conversation and its metadata
- **Profile:** an isolated configuration and credential scope
- **Provider:** an external model provider and its account/model capabilities
- **Workspace:** the allowed file and Git context for a session or run

The existing ownership modules remain authoritative during migration:

- process-local run state: `api/runtime_state.py`
- turn admission: `api/turn_admission.py`
- session persistence: `api/session_repository.py`
- event publication ordering: `api/run_event_sink.py`
- shared worker setup and teardown: `api/turn_execution.py`

Packaging these files under `runs/` or `sessions/` must preserve, not duplicate,
their ownership. The applicable contracts in `docs/rfcs/` continue to define
durability, recovery, replay, cancellation, and projection invariants.

## Configuration, profiles, and providers

These domains should be separated into three layers of knowledge:

1. **Configuration foundation:** parsing, validation, paths, immutable settings
   values, cache provenance, and persistence mechanics
2. **Profile scope:** selecting and describing the active isolated
   configuration home
3. **Provider behavior:** credentials, model discovery, quota, cost, and
   provider-specific adapters

A request or run should consume one resolved configuration snapshot. The code
that decides which profile/provider/model is active and the code that performs
the action must use the same resolved value. Runtime code should not repeatedly
query mutable global facades to reconstruct that decision.

## Compatibility seam

Compatibility exists to make migration safe; it must not become the permanent
internal architecture.

The repository currently contains multiple custom binding systems based on
context variables, `sys.modules`, facade-global lookup, rebinding, and reload
logic. They should converge on one temporary migration pattern:

- legacy external imports may be re-exported from the new package interface
- internal package code uses direct imports and explicit values
- tests cross the public interface or a real adapter seam
- every compatibility export has a known caller and a removal condition
- compatibility code must not own domain state or determine internal dispatch

The migration is complete when the compatibility layer can be deleted without
moving its complexity into many production callers.

## Target frontend structure

The frontend remains vanilla JavaScript with no build step. New and migrated
domains should use native ES modules with one entrypoint per domain:

```text
static/
  modules/
    boot/
      index.js
      navigation.js
      appearance.js
      composer.js

    messages/
      index.js
      send.js
      stream.js
      renderer.js
      approvals.js

    sessions/
      index.js
      repository.js
      sidebar.js
      lifecycle.js

    workspace/
      index.js
      navigation.js
      editor.js
      upload.js
```

Frontend rules:

1. Use semantic filenames, never numbered source fragments.
2. Every file must parse independently.
3. Do not split a function across files.
4. Prefer explicit imports and exports over global load-order coupling.
5. Keep one small compatibility adapter only for remaining inline HTML or
   legacy global callers.
6. Keep one authoritative static-asset inventory consumed by the page, service
   worker, and architecture tests.
7. Keep one coherent data module per locale. Locale size alone is not a reason
   to fragment translations.
8. Preserve the no-build-step constraint; native browser modules are sufficient.

## Test architecture

The interface of a module is its primary test surface. Tests should remain
valid when implementation files move inside the package.

Migration rules:

- prefer observable behavior and state-transition tests
- exercise public package interfaces rather than private facade globals
- retain focused source or AST checks only for invariants that cannot be proven
  behaviorally at reasonable cost
- point structural checks at the authoritative owner rather than a historical
  monolith filename
- cover every lifecycle exit relevant to the moved owner: success, error,
  cancellation, replacement, teardown, and concurrency where applicable
- verify legacy imports explicitly while a compatibility seam exists
- add architecture tests for package exports, forbidden dependency direction,
  cycles, frontend asset order, and state-owner identity

## Size and complexity policy

LOC is a review signal, not an acceptance criterion.

- A large implementation should trigger a cohesion and interface-depth review.
- A large data module, locale, table, or declarative catalog is not equivalent
  to a large orchestration module.
- A file should be split when it contains independently changing
  responsibilities with a clear internal seam.
- A file should remain together when splitting it would expose private state,
  duplicate ordering knowledge, divide a transaction, or require source
  assembly.
- New packages should not merely replace one monolith with a directory of
  shallow pass-through files.

The desired outcome is that large outliers have an explicit cohesive reason to
exist and a small interface, not that every source file is below an arbitrary
line count.

## Migration sequence

Each phase is a separate logical change or small series of related changes.
Behavior changes discovered during migration are fixed separately unless they
are required to preserve the moved interface.

### Phase 0: establish guardrails

- record the package and domain vocabulary from this plan
- inventory current package dependencies and cycles
- add a lightweight architecture check for newly forbidden imports and cycles
- define the single temporary compatibility pattern
- establish a dated baseline for the remaining facade callers

**Exit criteria:** New code cannot deepen the known dependency problems without
an explicit, reviewed exception.

### Phase 1: pilot the package pattern with updates

Convert the existing update facade and `update_*.py` modules into a real
`api/updates/` package. Preserve the public import path and update behavior.

This domain is the pilot because it is substantial enough to exercise policy,
repository, runtime, transaction, and compatibility concerns but is less
entangled than routes, streaming, sessions, or configuration.

**Exit criteria:**

- one small package interface
- semantic internal modules
- no domain-local `sys.modules` or facade rebinding
- legacy imports verified where still required
- update behavior and rollback tests pass through the package interface
- the pattern is documented well enough to reuse without inventing a second
  variant

### Phase 2: configuration, profiles, and providers

- convert each domain to the standard package pattern
- separate configuration foundations from profile and provider behavior
- pass resolved configuration snapshots into callers
- remove `config <-> profiles` and related provider cycles
- consolidate model catalog/cache ownership under the appropriate package

**Exit criteria:** Configuration foundations have no reverse dependency on
profiles/providers, and profile switching, provider discovery, cache
invalidation, and credential scoping remain behaviorally verified.

### Phase 3: runs and sessions

- group the existing run owners under `api/runs/`
- group session persistence, recovery, projection, sources, and events under
  `api/sessions/`
- preserve one owner for every state mutation and lifecycle transition
- remove route and transport imports from domain implementations
- carry all applicable RFC invariants through the new package interfaces

**Exit criteria:** A maintainer can trace one turn from admission through
execution, persistence, replay, and cleanup by following the run and session
packages without entering the HTTP router.

### Phase 4: HTTP router and routes

- introduce the HTTP router and per-domain route adapters
- move remaining domain behavior out of `routes.py` and `routes_parts/`
- preserve authentication, authorization, CSRF, error, and response semantics
- reduce `api.routes` to a temporary compatibility interface and then remove it
  when callers have migrated

**Exit criteria:** Route modules translate HTTP to domain calls and do not own
session, run, provider, workspace, or update state.

### Phase 5: streaming transport

- distinguish live transport from run execution and durable event ownership
- move local/Gateway orchestration behind the run package interface
- keep SSE framing, cursor handling, reconnect, and heartbeat behavior in the
  transport implementation
- eliminate reverse imports from run/session modules into streaming facades

**Exit criteria:** Changing the HTTP streaming transport does not require
changing run admission, execution, journaling, or session persistence.

### Phase 6: frontend modules

- choose one native ES-module loading pattern
- migrate one bounded domain first, retaining a narrow compatibility adapter
- move messages, sessions, workspace, boot, and panels incrementally
- centralize the static-asset inventory
- keep locale files as coherent data modules

**Exit criteria:** Frontend domains declare their dependencies explicitly,
individual files parse independently, and page/service-worker asset order is
derived from one source.

### Phase 7: test and compatibility cleanup

- migrate remaining source-shape tests to behavior or architecture contracts
- delete compatibility exports with no remaining callers
- remove obsolete `*_parts` directories and binding helpers
- enforce the final dependency rules for all migrated packages
- update the canonical architecture map to describe only current state

**Exit criteria:** The repository has one backend package convention, one
frontend module convention, no undocumented compatibility binder, and no known
forbidden package cycle.

## Verification required for every migration

Every package migration must show:

1. the previous and new public interface
2. all production and test callers found
3. dependency edges added and removed
4. state ownership and lifecycle exits affected
5. proof that relevant behavior tests exercise the new owner
6. legacy import verification while compatibility remains
7. focused tests plus neighboring subsystem tests through `./scripts/test.sh`
8. a full-suite run before the complete migration wave is declared finished
9. documentation updates when the current architecture or contributor workflow
   changes

Visible frontend changes additionally require desktop, narrow, and mobile
evidence under the repository's UI/UX guidance.

## Explicit anti-patterns

The following do not count as successful modularization:

- files named `_part_01`, `_part_02`, or similar extraction-order names
- a function body divided across files
- Python source stored in strings and executed or concatenated later
- independently invalid Python or JavaScript files
- empty facades that still require callers to know every internal function
- packages whose internal modules are routinely imported across domains
- wrappers that only forward arguments and add no invariant or leverage
- a new adapter with no second implementation or actual variation
- copying the same fallback, default, validation, or cleanup into parallel
  modules
- moving domain orchestration from `routes.py` into `routes_parts/` without
  changing ownership or dependency direction
- compressing formatting solely to lower a line count

## Completion criteria

The architecture program is complete when:

- backend domains use one recognizable package pattern
- public package interfaces are small and documented by behavior tests
- HTTP transport depends on domain modules, never the reverse
- configuration foundations have a one-way dependency direction
- run and session state each have explicit owners across every lifecycle exit
- custom facade/binding mechanisms have been removed or reduced to one
  temporary, caller-counted compatibility seam
- frontend domains use explicit native-module imports and one asset inventory
- package cycles and forbidden imports are checked automatically
- large remaining files are cohesive implementations with an explicit reason
  to remain together
- the canonical architecture documentation matches the live repository

## Immediate next step

Implement Phase 0 and design the `updates` pilot as one focused change. Before
moving files, inventory every current `api.updates` and `api.update_*` caller,
record the existing public exports and test seams, and define the atomic rename
sequence needed to replace `updates.py` with `updates/__init__.py` without a
partially importable intermediate state.

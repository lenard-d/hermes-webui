# Architecture audit — 2026-07-19

## Purpose and scope

This document records a read-only architecture audit of the current Hermes
WebUI repository at commit `7b76284c`. It covers repository shape, backend and
frontend structure, runtime and session state ownership, tests, documentation,
assets, and repository hygiene.

The audit used the tracked tree, static dependency and source analysis, the
project documentation and contracts, and a complete local test run. It is not
a proposal for one large rewrite. The findings are ordered by architectural
risk and expected leverage.

## Executive assessment

Hermes WebUI is not primarily suffering from a lack of code or tests. Its main
problem is accumulated coupling:

1. The lifecycle of a browser turn has no single owner.
2. Session truth is spread across multiple overlapping stores and projections.
3. The frontend is split into files but still behaves as one global script.
4. Central backend modules expose and mutate implementation details across
   nominal module seams.
5. Many tests are tightly coupled to source layout and private names.
6. Canonical-looking documentation contains stale snapshots and completed
   historical plans alongside current contracts.

Several project contracts and RFCs already describe the hardest runtime
problems accurately. The gap is mainly between those contracts and the current
implementation.

Large files alone are not the diagnosis. A large, deep module can still provide
a small interface and hide substantial implementation detail. The problematic
files here are both large and shallow: callers need to know their globals,
private helpers, storage details, or execution order.

## Repository inventory

Snapshot measurements:

- 1,718 tracked files
- approximately 42.7 MB of tracked content
- one Git pack containing 41,814 objects and occupying 160.34 MiB
- 65 files and approximately 96,401 lines under `api/`
- 93 files and approximately 107,408 lines under `static/`
- 1,293 files and approximately 329,836 lines under `tests/`
- 210 files and approximately 58,520 lines under `docs/`
- 37 tracked files at repository root

Dominant tracked file types:

- 1,365 Python files
- 178 PNG files
- 43 Markdown files
- 21 JavaScript files
- 60 bundled font files across TTF, WOFF, and WOFF2

Largest current files include:

| File | Approximate size | Lines |
| --- | ---: | ---: |
| `CHANGELOG.md` | 1.85 MB | 11,590 |
| `static/i18n.js` | 1.74 MB | 26,105 |
| `api/routes.py` | 1.14 MB | 26,746 |
| `static/ui.js` | 973 KB | 20,250 |
| `static/panels.js` | 627 KB | 13,212 |
| `api/streaming.py` | 560 KB | 11,318 |
| `static/style.css` | 512 KB | 7,240 |
| `api/config.py` | 461 KB | 9,799 |
| `static/sessions.js` | 438 KB | 9,362 |
| `api/models.py` | 427 KB | 9,621 |
| `static/messages.js` | 413 KB | 8,678 |
| `static/boot.js` | — | 3,869 |

`static/assistant_turn_anchors.js` is a useful counterexample. It is roughly
1,656 lines long, but exposes one frozen namespace and keeps most details
internal. It is substantially deeper than similarly sized global script files.

## Finding 1: browser-turn lifecycle has no single state owner

Priority: critical

Remediation status: in progress. `api/runtime_state.py` now owns process-local
liveness, admission blocking, immutable progress/cancellation snapshots, stale-worker
reconciliation, terminal cleanup, and copied views for route/model consumers.
`api/routes.py` and `api/models.py` no longer import the mutable transport,
worker, or event-cursor registries, and both local and Gateway producers record
the cursor through the runtime owner. `api/turn_admission.py` now owns the synchronous transition from an
accepted local turn through pending persistence, submitted journal entry,
stream publication, and worker launch. Agent interruption and cancelled-session
persistence consume the runtime snapshot in `api/streaming.py`; provider
execution, recovery, and final persistence still need to converge on the same
runtime Interface before this finding is closed.

Relevant areas:

- `api/routes.py`
- `api/streaming.py`
- `api/config.py`
- `api/models.py`
- `api/run_journal.py`
- `api/runtime_adapter.py`
- `docs/rfcs/hermes-run-adapter-contract.md`
- `docs/rfcs/webui-run-state-consistency-contract.md`

The lifecycle of a turn spans route handling, streaming threads, process-local
registries, session persistence, the durable journal, browser rendering, and
sidebar projections. Process-local state such as `STREAMS`, `SESSIONS`,
`CANCEL_FLAGS`, and agent instances is defined in `api/config.py` but read and
mutated from multiple modules.

`api/streaming.py` contains approximately 230 top-level definitions. Its
`_run_agent_streaming()` implementation is approximately 3,884 lines long and
combines provider execution, event delivery, persistence, tool state, titles,
compression, recovery, error handling, and cleanup.

The state-consistency RFC identifies eight overlapping state layers:

1. visible transcript
2. model context
3. pending metadata
4. live SSE and in-memory state
5. durable journal
6. compression and handoff state
7. browser DOM and caches
8. sidebar metadata

The run-adapter RFC describes the intended seam, but browser chat still relies
substantially on legacy in-process ownership. Restarts can orphan work,
reconnection remains process-local, and cancellation and stale writeback must
be handled repeatedly at different call sites.

### Deepening direction

Create one deep runtime module that owns the complete turn lifecycle: start,
event publication, persistence, cancellation, replacement, error, recovery,
and cleanup. Routes and browser code should depend on its interface rather than
on mutable runtime dictionaries or private streaming helpers.

This is the highest-leverage correctness change. It should start by tracing one
authoritative turn identity through:

`input -> normalize -> decision -> action -> persist -> cleanup`

Every lifecycle exit must be included: success, error, cancel, replacement,
disconnect, restart, and teardown.

## Finding 2: session truth and reconciliation are distributed

Priority: critical

Remediation status: in progress. `api/session_repository.py` owns the ordinary
full-load/lock/save mutation protocol and now reloads the current record after
acquiring the per-session owner lock, preventing a stale pre-lock object from
overwriting a newer transcript. Caller-provided objects seed only missing
records. CLI imports persist allowlisted source identity fields in the initial
write, and full plus metadata-only loads now preserve those fields. Session
deletion now runs under that same owner lock and hides cache, sidecar, backup,
index, tombstone, attachment, journal, terminal, completion-deduplication, and
non-messaging `state.db` cleanup behind one operation. It refuses deletion
while a turn is active, and admission rechecks the durable delete marker while
holding the same lock, closing both active-writeback and queued-start races.
Empty-sidecar and index-only-ghost cleanup is now a repository reconciliation
operation rather than route logic; it reloads candidates under their owner
lock, skips live turns, and removes recovery backups with deleted sidecars.
Local handoff-summary transcript markers now use the same mutation owner, so a
delayed handoff write cannot overwrite messages that arrived after its initial
read. Refreshing an already imported CLI session now fetches foreign data
outside the lock, then re-authorizes and merges it into the repository-current
record under the owner lock, preserving newer local turns. Recovery, migration,
broader reconciliation, and sidebar projections remain distributed.

Relevant areas:

- `api/models.py`
- `api/session_index.py`
- `api/session_db_adapter.py`
- `api/agent_session_db.py`
- `api/routes.py`
- `api/streaming.py`
- `docs/architecture/unified-session-db.md`

Session state is represented by JSON files, an index, agent `state.db`, caches,
stream state, tombstones, and derived sidebar metadata. Loading, repairing,
migrating, merging, deleting, and projecting sessions are not cleanly separated.

`api/models.py` contains approximately 222 top-level definitions. Despite its
name, it is not mainly a collection of data models. It includes session storage,
enumeration, CLI bridging, deletion, merging, and reconciliation. Its `Session`
class is approximately 595 lines long, and several reconciliation and CLI paths
are hundreds of lines each.

The unified-session-db document describes a dormant adapter spike. JSON remains
authoritative, live wiring is incomplete, and ownership questions are still
open. The document is also not prominently routed from the primary architecture
index.

### Deepening direction

Introduce one deep session repository that owns load, write, reconciliation,
tombstones, migration, and projections. Reading should not silently perform
unbounded repair. Other modules should depend on the repository interface and
should not know which file, database, cache, or index backs it.

Expected benefits are fewer sidebar/transcript inconsistencies, explicit repair
semantics, and a smaller change surface when storage evolves.

## Finding 3: backend module seams are not enforcing locality

Priority: high

### `api/routes.py`

Measured characteristics:

- approximately 590 top-level definitions
- approximately 135 module globals
- approximately 60 import statements
- 244 imported names from local `api` modules
- 62 imported names that are private by naming convention
- approximately 234 literal `/api/...` paths
- `handle_get()` is approximately 1,899 lines
- `handle_post()` is approximately 2,646 lines

The file is simultaneously a transport dispatcher, normalizer, policy engine,
orchestrator, persistence caller, and direct state mutator. Splitting it solely
by HTTP method or line count would leave the coupling intact.

### `api/config.py`

Measured characteristics:

- approximately 187 top-level definitions
- approximately 142 globals
- `get_available_models()` is approximately 1,839 lines
- `resolve_model_provider()` is approximately 372 lines

The module mixes configuration I/O, model catalog data, provider discovery,
routing decisions, locks, and mutable runtime registries. Its name understates
its role and makes it a dependency hub.

### Import topology

A static analysis including function-local lazy imports found one strongly
connected cluster containing 39 modules. Direct reciprocal relationships
include:

- `routes <-> streaming`
- `models <-> streaming`
- `config <-> profiles`
- `config <-> providers`
- `profiles <-> providers`
- `profiles <-> streaming`
- `auth <-> config`
- `auth <-> helpers`
- `auth <-> routes`
- `background_process <-> routes`

`routes` reaches roughly 54 local modules; `streaming` reaches roughly 24.
`config` is imported by roughly 27 modules and `profiles` by roughly 24. Lazy
imports often serve as an import-time workaround rather than as an intentional
architecture seam.

### Deepening direction

First establish deep runtime, session, provider-catalog, and configuration
modules with explicit interfaces. Then reduce route dispatch to transport work
and delegate cohesive behavior through those interfaces. The code that decides
an authoritative value and the code that acts on it must use the same resolved
value.

## Finding 4: the frontend has files, but not enforceable modules

Priority: high

Relevant areas:

- `static/index.html`
- `static/boot.js`
- `static/ui.js`
- `static/panels.js`
- `static/sessions.js`
- `static/messages.js`
- `tests/test_window_function_collision.py`

The main application uses classic deferred script tags in a fixed order.
Feature files communicate through top-level declarations and `window` state
rather than through explicit imports and exports.

Measured indicators:

- thousands of top-level function and variable declarations across the core
  scripts
- more than one thousand `window` references across the frontend
- 202 inline event handlers in `static/index.html`
- 362 inline `style` attributes in `static/index.html`
- duplicate helper names already exist across files

This has caused real production-bricking defects. The collision regression test
records cases where assigning `window.X` shadowed a top-level `function X()`.
Load order and global name selection are therefore part of the implicit
interface.

### Deepening direction

Move incrementally to native ES modules. This does not require a bundler or a
framework. Use feature-oriented modules with explicit imports and exports, and
put shared application state behind a deliberate interface. Replace inline
handlers as their owning feature moves.

This makes global name collisions structurally impossible and improves change
locality, test isolation, and navigation.

## Finding 5: localization and static presentation scale poorly

Priority: medium

`static/i18n.js` contains 15 locale objects and is loaded as one approximately
1.74 MB, 26,105-line script for every user. Static compression reduces network
cost, but not parsing, memory, merge conflicts, review cost, or contributor
contention.

`static/style.css` is approximately 7,240 lines, while substantial presentation
state also lives in inline HTML styles and JavaScript.

### Deepening direction

Move locale data into separate files with one shared fallback mechanism. Split
CSS and HTML only at stable feature seams. Do not copy fallback or theme logic
into each locale or feature.

## Finding 6: the test portfolio is large but structurally brittle

Priority: high

Remediation status: in progress. The initial figures below are the audit
baseline, not the current branch result. Source-shape assumptions touched by
the runtime/cache/session extraction have been replaced with behavioral tests.
After the deletion/admission and cleanup ownership work, a complete run of the current
13,484-test collection finished in 335.27 seconds with 13,326 passed, 158
skipped, 2 xfailed, 1 xpassed, and 34 subtests passed; there were no real
failures. The broader source-coupled portfolio and missing coverage threshold
remain open.

Collection through `./scripts/test.sh` found exactly 13,461 tests. The collector
reported that `hermes-agent` was unavailable locally, causing 30 agent-dependent
tests to be skipped at collection time.

The complete run finished in 401.94 seconds with:

- 13,292 passed
- 11 failed
- 158 skipped
- 2 xfailed
- 1 xpassed
- 34 subtests passed

The 11 failures were:

1. `test_render_messages_keeps_anchor_owned_turn_out_of_legacy_activity_rebuilds`
2. `test_forced_open_dom_is_not_cached_while_token_armed`
3. `test_render_messages_has_one_shot_virtual_blank_viewport_fallback`
4. `test_virtual_blank_viewport_recovery_evicts_stale_cache_before_fallback`
5. `test_boot_hydration_refreshes_chip_only_without_a_session`
6. `test_window_state_participates_in_cache_and_cached_button_is_rewired`
7. `test_boot_model_dropdown_explicitly_requests_profile_default_precedence`
8. `test_load_session_applies_pending_model_before_first_topbar_sync`
9. `test_boot_does_not_block_session_restore_on_model_catalog`
10. `test_failed_boot_model_catalog_prime_is_retryable`
11. `test_session_load_clears_stale_stream_before_response`

Most failures are caused by tests expecting exact source shapes, global helper
names, source windows, or call ordering. Examples include renamed cache helper
functions and exact string searches for inline code. One model-selection test
does express a potentially meaningful ordering regression, but it is also
implemented through source-position comparison and requires behavioral
confirmation.

Portfolio measurements:

- 1,278 `test*.py` files
- approximately 13,035 AST-visible test function definitions before parameter
  expansion
- 803 test files call `read_text()` on source or fixture files
- approximately 4,046 assertions compare strings against source, HTML, CSS, JS,
  or other loaded text
- 199 files import a combined 816 private backend names
- only eight test files mention Playwright
- roughly 222 files execute JavaScript through Node or another browserless
  harness
- roughly 109 files use an HTTP client
- roughly 357 files import backend modules
- more than 48 test files exceed 1,000 lines
- the largest test file is approximately 3,269 lines
- 21 exact duplicate test-body groups were found, covering 43 functions; this
  is visible but not a dominant source of size

There is no pytest coverage configuration, coverage dependency, fail-under
threshold, or current coverage report in the repository. Test count is not a
substitute for knowing which state-space paths are untested.

The suite also contains substantial strengths: HTTP behavior, concurrency,
state recovery, symlink and hostile-input handling, Node-executed behavior, and
browser smoke coverage. Those tests are better architecture anchors than
source-presence assertions.

### Deepening direction

Organize tests around module interfaces and observable behavior. Classify the
existing portfolio into behavior, contract, integration, browser, source-shape,
and historical reproduction tests. Convert high-value source assertions to
Node, HTTP, or browser behavior as the owning module is refactored. Add coverage
measurement first; choose enforcement only after the blind spots are understood.

Tests should be consolidated by durable behavior rather than accumulating one
large file per issue or sprint.

## Finding 7: lint debt remains in the old tree

Priority: medium

An informational whole-tree Ruff run reported 423 findings, including:

- 273 unused imports (`F401`)
- 61 assigned-but-unused values (`F841`)
- 27 redefinitions (`F811`)
- 16 `B009` findings
- 15 unnecessary f-strings (`F541`)
- 12 missing `raise ... from ...` links (`B904`)
- 11 `B010` findings
- 5 loop-variable binding findings (`B023`)

Approximately 333 findings were reported as mechanically fixable. A
production-focused run over the main Python entry points found 60 findings,
with the largest counts in `routes.py`, `server.py`, and `streaming.py`.

CI intentionally lints changed lines while treating the whole-tree backlog as
informational. This protects new code but does not reduce existing debt. Test
collection also emitted invalid-escape `SyntaxWarning`s in
`tests/test_update_banner_fixes.py`.

This debt is not the primary architecture problem. Cleanup should be scoped to
modules being deepened rather than mixed into one repository-wide refactor.

## Finding 8: canonical documentation is stale and layered ambiguously

Priority: medium to high

Remediation status: in progress. The root architecture, testing, and README
snapshots now use the repo test runner, the current 5-shard matrix, the refreshed
13,484-test/1,282-file count, and the current runtime/session/admission Module map.
The architecture roadmap now distinguishes initial file extraction from deeper
ownership. Archiving the embedded sprint logs and consolidating competing
architecture indexes remain open.

### `ARCHITECTURE.md`

The document presents itself as the canonical and exact current architecture,
but contains several stale snapshots:

- it named version `v0.51.792` from July 1 while the changelog currently reaches
  `v0.52.76` from July 18 plus Unreleased changes
- it described seven frontend modules while the current page now loads 15 main
  application scripts
- it reports `ui.js` at approximately 7,216 lines; the file is approximately
  20,250 lines
- it reports `panels.js` at approximately 6,480 lines; the file is approximately
  13,212 lines
- it reports `sessions.js` at approximately 3,517 lines; the file is
  approximately 9,362 lines
- it reports `messages.js` at approximately 2,301 lines; the file is
  approximately 8,678 lines
- it reports `boot.js` at approximately 1,607 lines; the file is approximately
  3,869 lines
- it marked frontend modularization as complete despite the continuing global
  script interface
- its "Current Endpoint Reference" explicitly reflects Sprint 1 / v0.3 and
  lists only a small subset of roughly 234 current literal endpoint paths
- its ADR-like section mixes superseded decisions, roadmap items, and current
  facts rather than maintaining a clear decision log
- its later architecture sprint logs include historical filesystem layouts and
  backup-oriented instructions

### `TESTING.md`

The document is approximately 1,981 lines and has accumulated chronological
sprint guidance:

- it claimed approximately 11,500 tests; the refreshed July 19 collection finds 13,484
- it claimed three CI shards; `.github/workflows/tests.yml` uses five shards for
  each of three Python versions
- its coverage reference reflects early sprints rather than the current suite
- an early section says browser and CSS checks are manual, despite current
  automated browser and JavaScript coverage
- a "last updated" marker refers to Sprint 2 even though later material extends
  through much newer sprints

### Other documentation observations

- The contracts and newer RFCs are generally more precise and honest than the
  root architecture snapshot.
- Multiple documents compete as architecture sources: `ARCHITECTURE.md`,
  `docs/CONTRACTS.md`, RFCs, phase inventories, and issue-specific documents.
- `BUGS.md`, `docs/ISSUES.md`, and
  `docs/architecture/unified-session-db.md` are not clearly routed from the
  primary documentation indexes.
- Internal local Markdown file links are generally healthy. A refined link
  scan found no confirmed broken local file links; apparent failures were code
  examples or URL patterns.
- `README.md` is approximately 837 lines and includes a large contributor
  snapshot plus a second notable-contributions section. Contributor counts
  conflict between the two sections, making at least one view stale.
- `CHANGELOG.md` is release-owned, approximately 1.85 MB, and receives very
  frequent micro-release updates. It is a repository churn hotspot even though
  ordinary contributor changes must not edit it.

### Deepening direction

Separate documentation by durability:

1. a concise current architecture map
2. durable contracts and invariants
3. dated ADRs with explicit status
4. executable setup and test guidance
5. an archive for sprint plans and historical snapshots

Generate or validate volatile counts such as test totals, shard counts, script
lists, and file-size claims. The README should route users to durable documents
rather than duplicate generated leaderboards.

## Finding 9: most media has no tracked consumer

Priority: medium

The repository contains 181 media files occupying approximately 15.06 MB. A
tracked-text reference scan found 172 files, occupying approximately 13.45 MB,
without a tracked consumer.

Breakdown:

- `docs/pr-media/`: 154 files, approximately 11.20 MB; all unreferenced by
  tracked repository text
- `docs/pr-assets/`: 2 files, approximately 313 KB; both unreferenced
- `docs/images/`: 12 files, approximately 2.26 MB; 10 unreferenced
- `docs/ui-ux/`: 6 files, approximately 1.03 MB; all unreferenced
- one exact duplicate media pair was detected under `docs/pr-media/2351/`

Unreferenced does not prove unused. Some images may be embedded directly in
external GitHub PR bodies. Therefore, deletion requires checking external PR
usage first.

The Git history also contains a historical `.graphify_cached.json` blob of
approximately 3.5 MB even though it is not in the current tracked tree. A
history rewrite would be destructive and is not justified by this audit alone.

### Deepening direction

Define an asset-retention policy:

- durable product documentation keeps only referenced assets
- temporary PR evidence lives outside the product tree or in an explicit
  archive with retention rules
- unreferenced assets are removed only after external PR usage is checked
- any history rewrite is a separate, explicitly approved operation

## Finding 10: the WebUI is directly coupled to agent internals

Priority: high, but partly external to this repository

`docs/architecture/agent-api-contract.md` accurately documents that the WebUI
and agent containers can share Hermes agent source and that WebUI code imports
agent internals, the agent state database, and provider runtime behavior
directly. A shared source mount is a deployment mechanism, not a stable
interface.

This makes upgrades sensitive to internal agent layout and increases the state
surface that WebUI must understand. The run-adapter and session-repository work
should also act as adapters around this external coupling so that the rest of
WebUI does not inherit it.

## Recommended order

The audit recommends addressing the findings in this order:

1. Complete runtime ownership for the browser-turn lifecycle.
2. Establish one session repository and reconciliation owner.
3. Replace the frontend global namespace with native module interfaces.
4. Deepen provider catalog and configuration modules, then slim route dispatch.
5. Rebalance tests toward observable behavior and add coverage visibility.
6. Reset architecture and testing documentation around durable sources.
7. Introduce an asset-retention policy and perform a separately reviewed
   cleanup.
8. Split localization and presentation files at stable feature seams.

Deleting assets or mechanically splitting large files first would improve
surface metrics without addressing the main correctness and change-locality
risks.

## Recommended immediate next step

Before moving code, create a focused design note for the current browser-turn
lifecycle. Trace one turn end to end through input, normalization, decision,
action, persistence, reconnect, and cleanup. Enumerate every lifecycle exit and
name the current owner of each state mutation. Use the existing run-adapter and
state-consistency RFCs as inputs.

That map will show which responsibilities can move behind one runtime interface
without turning the work into a repository-wide rewrite.

## Verification and limitations

Performed during this audit:

- complete tracked-file and size inventory
- source line and top-level definition counts
- local import graph and cycle analysis
- frontend global and inline-handler inventory
- tracked asset reference scan
- internal Markdown link scan
- informational Ruff analysis
- `./scripts/test.sh tests/ --collect-only -q`
- complete `./scripts/test.sh tests/` run

Limitations:

- `hermes-agent` was not available in the local test environment, so the
  agent-dependent subset was skipped
- no destructive local-state, Docker, deployment, or live-provider validation
  was performed
- unreferenced PR media was not checked against external GitHub PR bodies
- static import analysis includes lazy imports and therefore describes the
  reachable dependency topology, not necessarily simultaneous import-time
  execution
- this audit does not claim that every current failing test represents a
  product regression; several demonstrably represent stale test coupling

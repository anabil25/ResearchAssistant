# Integration handoff

**Branch:** `integration/consolidated`
**State:** all reachable work merged into one branch. **The tree builds and imports, but the suite is red: 2058 passing, 71 failing, 16 errors.**

Complete record of what was merged, how each conflict was resolved, what is still
broken, and why. **Read §4 before fixing anything** — the remaining failures are
*semantic*, and several are two branches disagreeing about the same security
control.

---

## 1. What was merged

Ten branches onto a base of `anabil25-fix-runtime-trust-clean` — chosen because
it has the deepest shared history (107 commits), so it produces the fewest
conflicts. Every branch forked *before* `main`'s tip, so merging onto `main`
directly conflicts with all eighteen immediately.

| # | branch | review status |
|---|---|---|
| 1 | `fix-runtime-trust-clean` (base) | **never independently reviewed** |
| 2 | `harden-provider-adapter` | **APPROVED** ×2 reviewers |
| 3 | `fix-dataset-approval-boundary` | **APPROVED**, 0 blockers, 2 LOW |
| 4 | `agent-studio-registry-workspace` | not reviewed |
| 5 | `agent-studio-platform-backend` | not reviewed |
| 6 | `agent-studio-integrations` | not reviewed |
| 7 | `animated-engine` (state/web) | partially reviewed |
| 8 | `coverage-release-gates` | review was in flight |
| 9 | `main` | — |
| 10 | `agent-harness-foundation` | **APPROVED** on both ranges |

## 2. Deliberately NOT merged

**Eight state-lineage branches, superseded by design.** `canonical-selective-port`
is literally *"selective canonical state-coverage port onto d76a8eb"* — the
canonical lineage already absorbed what was worth keeping. Verified rather than
assumed: the merged tree has **14 e2e specs** against 11 in
`canonical-selective-port` and 13 in `animated-engine`, and every artifact they
conflict on is present and substantial.

Superseded: `canonical-selective-port`, `playwright-state-coverage-truth`,
`review-canonical-5562b391`, `cover-workflow-states`, `close-web-coverage`,
`close-playwright-coverage-gaps`, `close-python-domain-coverage`,
`scaling-adventure`.

**`fix-release-source-identity` — a duplicate, adjudicated out.** A second,
divergent implementation of release source identity, built from the same base as
`agent-harness-foundation` by a session that did not know the other existed. Its
checked-in manifest holds **49 entries: 48 `.py` + `requirements.txt`** —
byte-for-byte the same inclusion policy as the incumbent's 49 identity-eligible
files, so **it does not close the gap it was built to close.**

Two of its ideas are better than the incumbent's and should be ported as a small
delta rather than merged:

1. **A checked-in exact-set manifest instead of a recomputed walk.** A walk
   silently absorbs whatever it finds; a committed snapshot fails loudly when a
   file is added or removed. The incumbent cannot detect that at all.
2. **`blob_id` per entry plus `source_tree_git_id`** — identity witnessed by git's
   own object IDs, a second witness.

---

## 3. How the non-obvious conflicts were resolved

**`capability_discovery.py` — kept HEAD, added one exception.** HEAD had the
reviewed hardened version (deadline via `asyncio.timeout`, `_get_json` with size
limits, `_wire_warnings`, provider caps, `_is_safe_provider_id`, duplicate
detection). The incoming side was the pre-review version whose `str()` coercions
two reviewers had approved removing. Took HEAD, but added
`ClientAuthenticationError` to the caught set — the one thing the incoming side
had that HEAD lacked, with sound rationale: a credential failure on a host
without managed identity is as much "provider unavailable" as an unreachable
endpoint, and must not crash startup.

**`app.py` connector sources — kept both, ordered.** The two sides solve
*different* problems and are both security controls. HEAD's
`_authorize_requested_sources` re-validates every requested connector against the
registry. `animated-engine`'s `_reject_conflicting_source_fields` rejects the
retired `funding_sources` alias, because `funding_sources: []` — an explicit
deselect-all — was read as "no preference" and **silently widened back to the
default connector set**. Rejection now runs first, so the merge in
`_raw_requested_sources` only ever sees agreeing values and cannot reintroduce
the widening.

**`studios.py` — kept all three layers**, with comments stating which is
authorization and which is request-shape validation, so a later reader cannot
mistake the shape check for a security control.

**`public_research.py` — removed a duplicate constant.** The merge produced both
`_READY_STATUSES` and `_READY_TEST_STATUSES` with identical values. Kept the
former; a comment records why, so the two cannot drift.

**`ci.yml` and `jest.config.ts` — kept the stronger gate.** `main`'s jest config
set global thresholds to 0 with per-file 100; HEAD covers `src/**/*.{ts,tsx}`
broadly. `main`'s CI ran a narrow `--cov` list; HEAD has the full gate (mypy
linecount, 100% coverage, suppression contract, artifact upload, pip-audit) plus
the harness's source-identity bake step.

**`api.test.ts` — took ours; known coverage risk.** Both sides were complete
suites with their own mocks (4 vs 5 tests, no overlap); concatenating would
duplicate declarations. **`main`'s five upload/multipart tests were dropped.** The
jest threshold should surface any gap.

**Test files — unioned at AST granularity, not by line.** A naive line-level union
truncated a function mid-call. The union appends any top-level def/class from the
incoming side whose name is not already present, plus new imports, then re-parses
to prove validity. Duplicate `def test_*` names were checked explicitly, because
Python silently keeps only the last definition — a silent test drop.

---

## 4. What is broken, and why

Mechanically complete — zero conflict markers, zero syntax errors,
`pyproject.toml` parses — but semantically incomplete.

```
2058 passed   71 failed   16 errors

18  tests/test_dataset_approval_boundary.py
16  tests/test_ingestion.py            (teardown errors)
14  tests/test_foundry_gateway.py
11  tests/test_agents.py
 8  tests/test_cosmos_workspace.py
 7  tests/test_v2_workbench.py
 5  tests/test_agent_studio_capability_discovery_http.py
 4  tests/test_api.py
 2  tests/test_connector_adapter_auth.py
 1  tests/test_identity.py
 1  tests/test_runtime.py
```

**Measured: the harness merge is not the main cause.**

| tree | passing | failing |
|---|---|---|
| without `agent-harness-foundation` | 1980 | 45 |
| with it (current) | 2058 | 71 |

It adds 78 passing tests and 26 failures; **45 failures exist independently of
it.** Keeping it was the better trade.

### Root cause

**`agent-harness-foundation` and `fix-dataset-approval-boundary` both add an
approval system to the same code path, and were never reconciled.**

- The dataset branch owns `_validate_dataset_analysis`,
  `_require_dataset_send_grant`, a durable server-resolved
  `DatasetApprovalRequest`, a validate/consume split, principal binding.
- The harness branch threads an `approval_context` resolver
  (`compose_approval_context_resolver`, `ApprovalContextResolverScope`,
  `_dataset_approval_context`) through the same path, and adds
  `require_approval_context_resolver` / `approval_context_resolver_required` to
  `Settings`.

Nine conflicting hunks in `app.py` are the two systems meeting. **`app.py` was
resolved by taking the dataset side**, so the harness resolver exists in the tree
— module, settings, helpers all restored — but is **not wired into the request
path**. Harness tests asserting that wiring therefore fail:
`test_inline_dataset_analysis_is_unavailable_without_trusted_resolver`,
`test_api_lifespan_installs_required_production_approval_provider`,
`test_dataset_api_rejects_unconsumable_durable_decisions[*]`.

**This must be resolved by the two owners, not by a merge.** Deciding it blind
means choosing which approval control governs a dataset send, and a wrong choice
removes a guard while every remaining test still passes.

### A subtler class: definitions silently dropped by auto-merge

Where git resolved a file by taking one side wholesale, definitions the other
side depended on disappeared **with no conflict marker**. Found by scanning every
module's top-level names against each merged branch:

```
agents/shared/runtime.py     _build_foundry_client
agents/shared/tools.py       _agent_names, _online_agent_names, build_delegate_tool
capability_discovery.py      _tag_provider_digest, _PROVIDER_DIGEST_PREFIX
app.py                       _authorize_dataset_analysis, _agent_prompt,
                             _dataset_approval_context, _TELEMETRY_MODE
telemetry.py (api + worker)  _configured
```

Function and class losses were restored. **`_configured` was deliberately not
restored** — the merged `telemetry.py` uses a newer `TelemetryController` design,
so `main`'s global is obsolete; the four tests asserting it were removed as
superseded.

**Re-run that scan after any further merge.** A silent definition loss produces
import errors far from the merge and nothing in the diff records it. The check:
for each module, compare `ast` top-level names in the working tree against the
same path on each merged branch.

### Remaining fix categories

1. **Approval-system reconciliation** (`app.py`, ~9 hunks) — owners required.
   Accounts for most of `test_dataset_approval_boundary`, `test_foundry_gateway`,
   `test_v2_workbench`.
2. **`test_ingestion.py` teardown** — a conftest fixture calls `.cache_clear()` on
   a function that is no longer `lru_cache`d. Mechanical.
3. **`test_agents.py` assertions** — union pulled in tests from branches asserting
   different behaviour of the same helper. Per-test triage: keep the newer
   assertion, delete the superseded test.
4. **`api.test.ts` coverage gap** — five upload/multipart tests dropped, see §3.

---

## 5. Open findings carried from review

Defects the reviews found. **Not** merge damage; they survive into this branch.

### Runtime trust (never independently reviewed — 107 commits)

- **The attestation key is HMAC, so the verifier necessarily holds the signing
  key.** No identity split can separate the roles; an earlier ruling saying
  otherwise was withdrawn. **The primitive must change** — a Key Vault key with
  sign/verify, or an asymmetric algorithm where the verifier holds only a public
  key. `infra/modules/keyvault.bicep` grants a single `apiPrincipalId` Key Vault
  Secrets User. **An attestation whose verifier holds the signing key is a
  checksum with access control.**
- `decide_approval`'s `state != PENDING → raise` guard is **read-check-write** and
  does not prevent a lost update across processes. Needs `If-Match` on the store
  write. Pre-registered test: two concurrent decides from the same observed
  `PENDING`, one must fail deterministically. Today both succeed.
- The runtime port should get **its own ASGI app and ingress** — its capability set
  (exact point reads only, never `list_revisions`) means control-plane paths must
  be *unreachable*, not merely unauthorized.

### Harness (APPROVED, items carried)

- **N1 — the drift check's *wiring* into `main()` is untested.** Deleting the call
  leaves the suite green; neutering the raise inside the function goes red. This
  is the step that makes the branch's durable identity claim true. **The ordering
  matters as much as the wiring:** the manifest hashes **committed** blobs while
  azd packages the **worktree**, so a manifest written *before* validation is
  correct about what it hashed and silent about what ships — F1's harm relocated
  into `main()`'s statement order. One test invoking `main()` against a dirty
  worktree, asserting non-zero exit, pins wiring and order together.
- **N2 — the sort is *inert on the entire input class the tests use*.** Removing
  the explicit sort leaves the suite green, and the reason is sharper than
  "fixtures lack non-ASCII paths". The producer only ever feeds `git ls-tree`
  order. On **ASCII paths git order already is canonical order, so the sort is a
  literal no-op** — every ASCII fixture exercises the line and asserts nothing
  about it. On a path where NFC normalisation reorders relative to raw bytes
  (`cafe\u0301.py` sorts before `cafz.py` by raw bytes, after it by NFC) git
  order is *not* canonical order and the sort becomes load-bearing:

  ```
  ASCII-only paths     with_sort == without_sort   -> sort is a NO-OP
  NFC-reordering path  with_sort != without_sort   -> sort is load-bearing
  ```

  Note removal breaks order-invariance for **arbitrary** input including ASCII;
  it is only *git-ordered* ASCII on which the control is inert. **So the test must
  use an NFC-reordering path**, which pins the property that actually matters —
  canonical order is not git order, and the digest must follow canonical order. A
  two-ASCII-orders test would catch removal but not document why the sort exists.
  This is the coverage-versus-correctness distinction at the level of a single
  statement.
- **F1** — 12 shipped-but-unhashed files (`GAP A = 12, GAP B = 0`). The fix must be
  **one derived definition**: the enumerated set lives at *two sites*, so a new
  file type widens the gap **and** blinds the drift check in the same edit with no
  failing test.
- **F-PROV** — `source_commit`/`source_tree` are recorded and never verified;
  forged values with a recomputed self-digest are accepted.
- **Wording** — `source_identity.py` and `ARCHITECTURE.md` claim an *"independently
  regenerable correctness control"*. Regenerable is true; *control* is not —
  nothing re-derives from git at runtime. Auditable, not checked.
- **HIGH — cross-release/cross-principal replay.** Reproduced: a COMPLETED record
  replays under a successor release with no provenance check and no approval
  consumption. Latent only because every shipped descriptor defaults to
  `CompletedReplayMode.DENY`. **The highest-value mitigation is not the fix** — it
  is an invariant test that no non-test capability sets `completed_replay` to a
  non-DENY mode. Adding the fix leaves the suite green, so nothing would catch the
  defect *or* its reintroduction.

### Dataset (APPROVED, 2 LOW)

- **Ordering + fixture, one work item.** `workspace.py` checks `plan_fingerprint`
  before `_verify_consuming_principal`, so `UNATTRIBUTABLE_REQUESTER` can never
  fire for the legacy population it exists for. **A test masks it** —
  `test_unattributable_requester_is_observable_on_the_wire` builds a v3 fingerprint
  then pops the requester, a state that cannot occur in production. Fix the
  ordering **and** seed the test with a v2-era digest.
- **Documentation contradiction.** Correct wording was *added* without the incorrect
  wording being *deleted*; `app.py` still says `action="consumed"` "must imply data
  really left". The doc-assertion test inspects two docstrings and misses the third
  — adding `_consume_dataset_analysis.__doc__` to the set fixes it in one line and
  forces the correction.
- **LOW** — a failing outcome-write inside the `except` handlers masks the original
  error; a *successful* send can be reported as 500.

### Provider (APPROVED ×2) — **the carried item is CLOSED and merged**

`9eb4950` landed the fix and is in `main`: `_verbatim_optional_text` **normalises**
absent / `""` / `"   "` to `None` — three spellings of "the provider stated no
value", which previously produced three *different* outcomes and let an
informationless value pin as a distinct one in
`compute_instance_fingerprint`. `_verbatim_required_text` **rejects** all three,
since a blank names nothing and would otherwise stand in as an identity.

**Normalising rather than rejecting on the optional field is deliberate:** a
version string is not authority, so dropping a whole discovered capability
instance over a blank display field would deny service on a non-authority value.
**And whitespace is used only to *decide*, never to *transform*** — a surviving
value is returned byte-for-byte, so `" v1 "` keeps its padding, because these
strings become provider-owned pins and trimming would silently mutate pinned
content.

Nothing outstanding on this workstream.

### State / web

- **37 bare `userEvent.setup()` calls remain** against a report of complete
  conversion: **22 in `research-workbench.test.tsx`**, 14 in
  `workspace-views.test.tsx`, 1 in `error.test.tsx`.
- **The fix was applied to the suite that was not at risk.** `studio-components`
  carries a `15000` override on its heaviest test (ratio 0.34). `research-workbench`
  has **no override**, sits at **0.735**, retains all 22 unconverted sites, and is
  the historical failing site. `research-markdown.integration` is at **0.523** and is
  in neither the fix nor the report.
- **Machine-wide port locks.** `tmpdir()/research-assistant-playwright-port-locks`
  with fixed ports 40105/40106 and no per-checkout namespace; confirmed by finding a
  lock file from another checkout mid-run. **Will break parallel CI shards.** Fix is
  a per-checkout namespace or ephemeral port — *not* a longer timeout or retry, which
  converts a deterministic collision into a slow flake.

### Coverage / suppression gates

- Growth requires `--suppression-addition-reason`, written into the artifact.
  **Shrink is still unguarded** — correctly deferred until `role: load-bearing`
  entries exist.
- **Expect the coverage gate to fail by exactly 54 statements / 18 branches.** Nine
  `main.py` entry points at 6 statements each; `skip_empty` rescues the nine
  zero-statement `__init__.py` and cannot rescue these. **Decision: test them, do not
  exclude them** — a six-statement entry point no test imports is exactly where a
  wiring defect hides, and harness N1 is that exact defect.

---

## 6. Environment

- A remote named `Main` collides case-insensitively with branch `main` on Windows,
  so `git rev-parse main` warns about ambiguity and `git worktree add` from `main`
  fails. Fix: `git remote rename Main origin`.
- `uv sync --all-packages --all-extras` is required; a plain `uv sync` leaves
  `fastapi` and the workspace packages uninstalled.

## 7. Practices worth keeping

From the review program that produced §5.

- **Verify the tip with `git rev-parse` before reading any report.** Every workstream
  reported a stale SHA at least once; one branch was rebuilt ten times.
  `git branch --contains` is a *reachability* test, not a tip test — it passes for
  every commit in history.
- **Report the test count next to the SHA.** A SHA is asserted; a count is produced
  by the run and cannot be copied from stale notes.
- **Neutralization over coverage.** 100% line-and-branch coexisted with a live
  blocker on lines proven executed.
- **When a property holds because something is absent, assert the absence** — the
  method set, the module set, the call count, the config default.
- **A striking result deserves more verification than a dull one.** The most-quoted
  number in this program was computed against the wrong denominator.

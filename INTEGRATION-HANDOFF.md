# Integration handoff

**Branch:** `integration/consolidated` @ `d421a97`
**Created:** 2026-07-24, by a coordination session that reviewed ~10 parallel workstreams.
**Status:** partial merge. 4 of 18 branches merged cleanly. 14 conflict.

Read this before touching anything. The short version: **most of these branches
are reviewed and approved, the conflicts are mechanical rather than
disagreements, and the two files that will hurt you are `app.py` and
`generated-api.ts`.**

---

## 1. What is already merged into `d421a97`

Merged in this order, all clean:

| branch | tip | status |
|---|---|---|
| `anabil25-fix-runtime-trust-clean` | `540312b` | base (107 commits) — **not reviewed** |
| `anabil25-harden-provider-adapter` | `9eb4950` | **APPROVED** by two independent reviewers |
| `anabil25-fix-dataset-approval-boundary` | `1affe85` | **APPROVED**, 0 blockers, 2 LOW |
| `anabil25-agent-studio-registry-workspace` | `5603972` | not reviewed |

**Why runtime-trust is the base and not `main`:** every branch forked before
`main`'s tip `a62cd0a`, so merging onto `main` conflicts immediately with all 18.
Using the branch with the deepest shared history (107 commits) minimises
conflicts. **`main` still needs merging in — it conflicts with 10 hunks across 5
files.**

---

## 2. What did not merge, ordered by difficulty

Re-derive with `git merge --no-edit <branch>` then `git merge --abort`.

| branch | files | hunks | conflicting files |
|---|---|---|---|
| `agent-studio-platform-backend` | 1 | 2 | `capability_discovery.py` |
| `agent-studio-integrations` | 3 | 3 | `main.bicep`, `resources.bicep`, `test_connector_adapter_auth.py` |
| `close-python-domain-coverage` | 1 | 3 | `api.test.ts` |
| `close-playwright-coverage-gaps` | 2 | 4 | `api.test.ts`, `generated-api.ts` |
| **`main`** | 5 | 10 | `workspace-views.tsx`, `api.test.ts`, `app.py`, `studios.py`, `test_v2_workbench.py` |
| `scaling-adventure` | 4 | 11 | `api.test.ts`, `test_cosmos_workspace.py`, `test_identity.py`, `test_v2_workbench.py` |
| `close-web-coverage` | 4 | 12 | `route.test.ts`, `workspace-views.*`, `api.test.ts` |
| **`agent-harness-foundation`** | 7 | 18 | `generated-api.ts`, `app.py`, `config.py`, `studios.py`, `conftest.py`, `test_agents.py`, `test_v2_workbench.py` |
| `fix-release-source-identity` | 7 | 18 | *(identical set — see §5, this is a duplicate)* |
| `coverage-release-gates` | 8 | 21 | web tests + `test_cosmos_workspace.py`, `test_identity.py`, `test_v2_workbench.py` |
| `cover-workflow-states` | 9 | 22 | as above + `research-workbench.tsx` |
| `playwright-state-coverage-truth` | 11 | 32 | as above + `url-policy.*` |
| `canonical-selective-port` | 12 | 34 | as above + `public_research.py` |
| `review-canonical-5562b391` | 15 | 39 | as above + `*.spec.ts` |
| `animated-engine` | 16 | 40 | largest; adds `app.py` |

**The conflicts cluster, which is good news.** Four files account for most of
them: `app.py`, `generated-api.ts`, `api.test.ts`, `test_v2_workbench.py`.
Resolve those four consistently and most branches fall in behind.

**`generated-api.ts` is generated — do not hand-merge it.** Take either side,
then regenerate: `cd apps/web && npm ci && npm run generate:contracts`. Several
workstreams verified it regenerates byte-identical.

**Suggested order:** smallest first (`agent-studio-platform-backend`), then
`main`, then the web-test cluster together, then `agent-harness-foundation`
last — its `app.py`/`config.py`/`studios.py` hunks are the semantically hardest.

---

## 3. Do not blind-resolve these

`app.py`, `config.py`, `studios.py` and `workspace.py` carry **authorization
boundaries, fail-closed guards, and approval-consumption logic** that were
individually argued and tested during review. A wrong resolution silently
removes a guard and every test still passes — that failure mode was found
repeatedly across these branches.

**When resolving a hunk in those files, find the review finding that produced it
(§4) before choosing a side.**

---

## 4. Open work, per workstream

### Runtime trust (`540312b`) — merged, **not reviewed**, has the most serious open finding

- **The attestation key is HMAC**, so the verifier necessarily holds the signing
  key. **No identity split can separate the roles** — an earlier ruling that said
  otherwise was withdrawn. An attestation whose verifier holds the signing key is
  a checksum with access control. **Fix requires changing the primitive**: a Key
  Vault key with sign/verify (private material never leaves the vault), or an
  asymmetric algorithm where the verifier holds only a public key.
  `infra/modules/keyvault.bicep` grants a single `apiPrincipalId` Key Vault
  Secrets User. **Unactioned; nobody is tracking it.**
- `decide_approval`'s `state != PENDING → raise` guard is **read-check-write**
  and does not prevent a lost update across processes. Needs `If-Match` on the
  store write. Pre-registered test: two concurrent decides from the same observed
  `PENDING`, one must fail deterministically. Today both succeed.
- Mount decision: the runtime port should get **its own ASGI app and ingress**,
  because its specified capability set (exact point reads only, never
  `list_revisions`) makes control-plane paths need to be *unreachable*, not merely
  unauthorized.
- This branch is 107 commits and **has never been independently reviewed.**

### Provider adapter (`9eb4950`) — merged, **APPROVED** ×2

- **Open fix:** `_verbatim_optional_text` should **normalise** `""` and `"   "` to
  `None` (matching the already-accepted absent case); `_verbatim_required_text`
  should **reject** both. Currently `""` rejects the whole instance while `"   "`
  is accepted *into a content digest* — three semantically identical inputs, three
  outcomes. Pin all four cases per field.

### Dataset boundary (`1affe85`) — merged, **APPROVED**, 2 LOW open

- **Ordering + fixture, as one work item:** `workspace.py` checks
  `plan_fingerprint` (~L1024) before `_verify_consuming_principal` (~L1044), so
  `UNATTRIBUTABLE_REQUESTER` can never fire for the legacy population it exists
  for. **And a test masks it** —
  `test_unattributable_requester_is_observable_on_the_wire` builds a v3
  fingerprint then pops the requester, a state that cannot occur in production.
  Fix the ordering **and** seed the test with a v2-era digest; either alone leaves
  the system lying.
- **Documentation contradiction:** correct wording was *added* without the
  incorrect wording being *deleted*. `app.py:1444` still says
  `action="consumed"` "must imply data really left". The doc-assertion test
  inspects two docstrings and misses this third one — adding
  `_consume_dataset_analysis.__doc__` to the set fixes it in one line and forces
  the correction.
- **LOW:** a failing outcome-write inside the `except` handlers (`app.py`
  ~`:1581`, ~`:1749`) masks the original error — a successful send can be
  reported as 500.

### Harness (`34543f1`) — **NOT merged**, **APPROVED** on both ranges

57 probes, 24 mutations across three independent passes. Carried items:

- **N1 (raised above medium):** the drift check's *wiring* into `main()` is
  untested — deleting the call leaves the suite green, while neutering the raise
  inside the function goes red. This is the step that makes the branch's durable
  claim true.
- **N2:** permutation-invariance holds only because `git ls-tree` emits sorted
  paths. An accident, not an invariant.
- **F1:** 12 shipped-but-unhashed files (`GAP A = 12, GAP B = 0`). The fix must be
  **one derived definition**, not two synchronised copies — the enumerated set
  lives at two sites, so a new file type widens the gap *and* blinds the drift
  check in the same edit with no failing test.
- **F-PROV:** `source_commit`/`source_tree` are recorded and never verified —
  forged values with a recomputed self-digest are accepted. Needs one docstring
  sentence.
- **Wording:** `source_identity.py` L43 and `ARCHITECTURE.md` claim an
  *"independently regenerable correctness control"*. Regenerable is true;
  *control* is not — nothing re-derives from Git at runtime. Auditable, not
  checked.
- **HIGH gate-readiness — cross-release/cross-principal replay.** Reproduced:
  a COMPLETED record replays under a successor release with no provenance check
  and no approval consumption. Latent only because every shipped descriptor
  defaults to `CompletedReplayMode.DENY`. **Reverse neutralization showed adding
  the fix leaves the suite green**, so nothing would catch the defect *or* its
  reintroduction. **Highest-value mitigation is not the fix** — it is an invariant
  test that no non-test capability sets `completed_replay` to a non-DENY mode.

### Coverage/suppression gates (`a781298`) — **NOT merged**, independent review in flight

- Suppression contract with an exact-set baseline; growth requires
  `--suppression-addition-reason`, and the reason is written into the artifact.
- **Shrink is still unguarded** — a baseline shrink passes silently if the source
  suppression is genuinely removed. Correctly deferred: it only matters once
  `role: load-bearing` entries exist, which is nobody yet.
- Mypy domain exact at 101 files; coverage 56/56 across 15 derived roots.

### State / web (`9d7b90b`) — **NOT merged**, largest conflict set

- **37 bare `userEvent.setup()` calls remain**, against a report of complete
  conversion: **22 in `research-workbench.test.tsx`**, 14 in
  `workspace-views.test.tsx`, 1 in `error.test.tsx`.
- **The fix was applied to the suite that was not at risk.**
  `studio-components` carries a `15000` override on its heaviest test (ratio
  0.34). `research-workbench` has **no override**, sits at **0.735**, retains all
  22 unconverted sites, and is the historical failing site.
  `research-markdown.integration` is at **0.523** and is in neither the fix nor
  the report.
- **BLOCKER: machine-wide port locks.** `tmpdir()/research-assistant-playwright-port-locks`
  with fixed ports 40105/40106 and no per-checkout namespace. Confirmed by finding
  a lock file from another checkout mid-run. **Will break parallel CI shards.**
  Fix is a per-checkout namespace or ephemeral port — *not* a longer timeout or a
  retry, which would convert a deterministic collision into a slow flake.

---

## 5. `fix-release-source-identity` is a duplicate — do not merge it

`e121e41` is a **second, divergent implementation of release source identity**,
built from the same base as `agent-harness-foundation` by a session that did not
know the other existed. Adjudicated by measurement:

- Its checked-in manifest contains **49 entries: 48 `.py` + `requirements.txt`** —
  **byte-for-byte the same inclusion policy** as the incumbent's 49
  identity-eligible files. **It does not close the gap it was built to close.**
- Its conflict set against the integration branch is *identical* to the harness's.

**Two of its ideas are better than the incumbent's and should be ported as a small
delta rather than merged wholesale:**

1. **A checked-in exact-set manifest instead of a recomputed walk.** A walk
   silently absorbs whatever it finds; a committed snapshot fails loudly when a
   file is added or removed. The incumbent cannot detect that at all.
2. **`blob_id` per entry and `source_tree_git_id`** — identity witnessed by git's
   own object IDs, a genuine second witness.

Its commit `c90b9ce` ("refuse to deploy a worktree that diverges from packaged
identity") targets the same weakness as harness N1.

---

## 6. Known integration facts

- **The merge will fail coverage by exactly 54 statements / 18 branches.** Two
  branches carry `fail_under = 100` over all ten agent packages. `skip_empty`
  rescues the nine zero-statement `__init__.py` and cannot rescue the nine
  `main.py` at 6 statements each. **Decision required before merging: test them,
  do not exclude them** — a six-statement entry point that no test imports is
  exactly where a wiring defect hides, and harness N1 is that exact defect.
- **The same merge closes the mypy hole.** The gates branch sets
  `files = ["packages","services","agents","scripts","tests"]` — the whole tree —
  so 27 previously-unchecked files come into the domain. Coverage breaks, mypy
  heals.
- **Environment defect:** a remote named `Main` collides case-insensitively with
  branch `main` on Windows, so `git worktree add` from `main` fails and
  `git rev-parse main` warns about ambiguity. Fix:
  `git remote rename Main origin`.

---

## 7. Working notes for whoever picks this up

Two documents in the coordination session's folder are worth reading:
`review-standards.md` (~60 KB, named defect shapes and measurement rules) and
`integration-criteria.md` (~22 KB, eight blockers with re-check commands).

The habits that produced every finding here:

- **Verify the tip with `git rev-parse` before reading any report.** Every
  workstream reported a stale SHA at least once; one branch was rebuilt ten times.
  `git branch --contains` is a *reachability* test, not a tip test — it passes for
  every commit in history.
- **Report the test count next to the SHA.** A SHA is asserted; a count is
  produced by the run and cannot be copied from stale notes.
- **Neutralization over coverage.** 100% line-and-branch coexisted with a live
  blocker on lines proven executed. Break each guard and confirm a specific test
  goes red.
- **When a property holds because something is absent, assert the absence** — the
  method set, the module set, the call count, the config default.
- **A striking result deserves more verification than a dull one.** The most
  quoted number in this program was computed against the wrong denominator.

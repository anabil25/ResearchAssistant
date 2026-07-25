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
| 3 | `fix-dataset-approval-boundary` | **APPROVED — but of a tree that did not ship.** Verdicts cover `c5fad2e`/`cfb9366`/`10d3e39`; `1affe85` merged, **+168 lines of `workspace.py` authorization code after the last review**. Do not cite as reviewed — see §5 |
| 4 | `agent-studio-registry-workspace` | not reviewed |
| 5 | `agent-studio-platform-backend` | not reviewed |
| 6 | `agent-studio-integrations` | not reviewed |
| 7 | `animated-engine` (state/web) | partially reviewed |
| 8 | `coverage-release-gates` | review was in flight |
| 9 | `main` | — |
| 10 | `agent-harness-foundation` | **APPROVED** on both ranges |

**Branches are retained deliberately — do not prune them.** They are the evidence
base for every merge and exclusion decision in this document; deleting them makes
§2's claims unverifiable. Every branch other than
`fix-release-source-identity` (excluded by design) now carries **zero files
`main` lacks**.

**Two integration tools live on `integration/with-harness` and were deliberately
not merged:** `find_dropped_defs.py` and `restore_dropped_defs.py`, the AST scan
that recovered six silently-dropped Python definitions. They are kept out of
`main` because they are scratch tooling, and because **the scan has the known
blind spot documented in §3 — it enumerates `.py` only**, which is exactly why it
could not see the release gates being deleted from `ci.yml`. Recover and widen
them before reuse; do not run them as-is and treat a clean result as coverage.

## 2. Deliberately NOT merged

**Twelve state-lineage branches, now genuinely superseded — but the original
reasoning here was wrong and was corrected by re-measurement.**

The first version of this section argued supersession from a **count**: "the
merged tree has 14 e2e specs against 11 in `canonical-selective-port` and 13 in
`animated-engine`." **A count is not a set.** Re-measured by comparing file
*sets*, the spec lists were **complementary**: `main` had 2 specs those branches
lacked, and they had **3 `main` lacked** —
`state-data-research.spec.ts` (19 tests), `state-literature-grant.spec.ts` (6),
`state-workspace.spec.ts` (10). The branch carried *both*
`literature-state-closure` and `state-literature-grant`, so they were additional
specs, not renames. **35 tests were being discarded on a counting argument.**

Those three are now merged (`8aedbc0`), with the missing `formatTime` /
`statusLabel` exports in `workspace-views.tsx` restored — `main` did not
typecheck before that commit.

**Supersession is now established the right way: by file set, at every branch.**
**Re-measured at `7ee1b97`: 15 branches carry unmerged commits; 14 of them add
zero files `main` lacks.** The sole exception is `fix-release-source-identity`
with 4 files (`agents/shared/source_manifest.py`, `agents/source_manifest.json`,
`scripts/build_source_manifest.py`, `tests/test_source_manifest.py`) — excluded by
adjudication, not oversight, and its author concurred. On the shared files they
differ over,
`main`'s versions are newer and larger (e.g.
`verify-playwright-runtime-coverage.mjs` 310 lines vs 244;
`run-e2e-coverage-gate.mjs` 242 vs 119) — and
`policy-gated-external-link.test.tsx` is byte-identical.

> **Measurement note, because the wrong query gives an alarming answer.**
> `git diff --diff-filter=A main...<branch>` compares **merge-base → branch**, so a
> file added on *both* sides is still reported as added by the branch. Run that way
> the branches appear to hold **37** files `main` lacks. Testing actual membership
> (`git ls-files`) gives **4**. The 33-file gap is entirely files both lines wrote
> independently. **Ask "is this file in `main`?", not "did this branch add it?"** —
> and note the error ran in the alarming direction, in the final accounting, after
> that asymmetry had already been recorded in §7.

Superseded: `canonical-selective-port`, `playwright-state-coverage-truth`,
`review-canonical-5562b391`, `cover-workflow-states`, `close-web-coverage`,
`close-playwright-coverage-gaps`, `close-python-domain-coverage`,
`scaling-adventure`, `cover-literature-grant-states`, `cover-settings-states`,
`state-coverage-close-gaps`, `state-data-research-coverage`.

**`fix-release-source-identity` — a duplicate, adjudicated out.** A second,
divergent implementation of release source identity, built from the same base as
`agent-harness-foundation` by a session that did not know the other existed. Its
checked-in manifest holds **49 entries: 48 `.py` + `requirements.txt`** —
byte-for-byte the same inclusion policy as the incumbent's 49 identity-eligible
files, so **it does not close the gap it was built to close.**

**Do not port its manifest wholesale.** An earlier draft of this document
credited three of its mechanisms too generously; corrected here after measuring
the code rather than reading its description.

1. **`source_tree_git_id` is *not* a git tree object ID.** Measured:
   `_source_tree_git_id(entries) = canonical_digest([[path, blob_id] …])` — a
   **synthetic SHA-256 over filtered `(path, blob_id)` pairs**. The per-entry
   `blob_id`s *are* real git object IDs and are worth adopting; the aggregate is
   not, and calling it a git-object witness overstates it.
2. **Its recompute-on-parse is a self-consistency check, not external
   verification.** It proves entries ↔ digest agree, so a truncated or edited
   manifest fails closed — genuinely stronger than the incumbent's single
   self-digest. But it does **not** verify the entries against a real commit, so
   it is the same *category* as the incumbent's self-digest rather than the answer
   to **F-PROV**. An earlier draft said it was; that was wrong.
3. **A checked-in manifest cannot name the commit that contains it** without
   recursion, which is why this one omits commit identity entirely. A
   build-produced manifest is generated *after* the commit exists and is excluded
   from identity, so it can name the exact `source_commit` and the real `agents/`
   subtree object. **That naming is the property worth keeping.**
4. **Staleness has no runtime guard.** Its freshness check exists only in CI
   (`build_source_manifest.py --check`); there is no predeploy hook and its
   `azure.yaml` exposes only `postdeploy`. A direct `azd deploy` can therefore
   ship changed worktree code alongside an old, internally-valid manifest — a
   trustworthy-looking identity that describes something else.
   **↳ Closed on that branch at `c90b9ce`, verified. The branch tip is
   `e121e41`, not `c90b9ce`.** Three commits sit on `b7969d6`
   (`76e8812` → `c90b9ce` → `e121e41`), none reachable from `main`.
   **But porting from the two cited commits is safe — an earlier revision of this
   note overstated the risk.** Verified directly at `76e8812`: all three
   validators are present in complete form ("source manifest git identity does not
   match its entries", "source tree digest does not match its entries", "source
   manifest digest does not match its content"), along with `blob_id` (5
   occurrences), `source_tree_git_id` (9) and `inclusion_policy_version` (13).
   **Every mechanism credited to this branch is recoverable from `76e8812` alone.**
   `e121e41` adds only the developer-experience layer on top — fail-closed errors
   naming the producer command, an offline-harness path that loads the packaged
   manifest instead of raising a bare `TypeError`, plus README/ARCHITECTURE docs
   and tests. Worth having, not required for a port.
   It now registers a
   `predeploy` hook (`scripts/predeploy.{ps1,sh}`) running
   `--verify-worktree --check`, so the hook set is
   `{predeploy, postdeploy, preprovision, postprovision}`. **This objection is no
   longer a reason to prefer the incumbent** and is recorded as closed so the
   verdict does not keep being cited after it stopped describing the code.

   **↳↳ And the incumbent is stronger here than the branch that raised it.**
   `main`'s `main()` calls `validate_worktree_matches_commit(...)` **with no
   guarding flag**, and both predeploy scripts invoke
   `python -m scripts.build_agent_source_tree` **with no arguments** — so
   divergence is gated **by default**. The branch put the same check behind an
   opt-in `--verify-worktree`. The branch author verified this themselves and
   withdrew their own standing concern about the `rglob` in
   `worktree_source_entries` — it walks the worktree *specifically to compare it
   against the committed set*, which is the honest use, not a fallback.
   **A concern raised twice against the incumbent was wrong both times, and the
   incumbent's design was better on the exact axis it was challenged on.**
   It does **not** disturb objections 1, 2, 3 or 5, which are structural.
   Note also what it does *not* change: **the incumbent already had both** a
   `predeploy` hook and a worktree-divergence gate
   (`validate_worktree_matches_commit`), so the addendum reaches parity with this
   branch rather than passing it.
5. **Its checked-in set is not the package set.** Its `.agentignore` does not
   exclude package-eligible non-Python files (`.agent_configs/baseline/metadata.yaml`,
   the root `*.eval.yaml`, `datasets/**.jsonl`, `evaluators/**.{json,yaml}`), so
   the snapshot **serializes the same incomplete filter**. F1 is untouched.

**Better synthesis, and the recommended shape:** keep `build_agent_source_tree.py`
and the uncommitted `.release/source-tree.json` as build-produced provenance.
Solve F1 with a **separate** checked-in, versioned package-inclusion contract
derived from the actual azd package rules, and have **predeploy compare three
things**: (a) actual package-eligible worktree files, (b) committed git blobs, and
(c) that approved exact set — *before* generating the runtime manifest. That takes
the exact-set mechanism's real benefit (an unexpected topology change fails loudly)
without accepting stale or self-referential release evidence.

**Worth adopting selectively:** per-entry `blob_id` content evidence, and its
**stray-file immunity proof** — committed LF, re-checked-out under
`core.autocrlf=true` so worktree bytes were genuinely CRLF, then untracked,
ignored and backup files dropped in; producer output byte-identical. That is a
stronger demonstration than any fixture, because it proves the producer reads git
objects rather than the worktree.

**Adopt `blob_id` as an *addition*, never as a replacement — the two identities
are not interchangeable.** Verified in the incumbent producer: `_normalized_source`
(L79) reads content with `newline=None` (L85), i.e. universal-newlines
translation, and paths are NFC-normalised at L76 with a collision check at L101.
So **the logical digest deliberately normalises newline conventions, while a git
`blob_id` witnesses the raw committed bytes.** Swapping one for the other would
silently change what the identity *means*, and an LF/CRLF checkout difference
would then produce a different release identity. Note this is also *why* the
stray-file immunity proof above passes: byte-identical output under
`core.autocrlf=true` is a consequence of that normalisation, so the two facts
corroborate rather than being independent evidence.

**And the incumbent's tree identity is real, checked the same way the rival's was
disproved.** `build_agent_source_tree.py` L225–230 computes
`git rev-parse --verify {commit}:{source_root}` — an actual **git tree object ID**
for `agents/` at the named commit — alongside a real `source_commit` (L149) and a
*separate* `source_tree_digest` (L231). Three fields, no overloading. That is the
concrete asymmetry behind objection 1: the incumbent names real git objects
because it runs after the commit exists; the rival's single `source_tree_git_id`
is a synthetic SHA-256 over `(path, blob_id)` pairs.

**One concrete, cheap improvement worth porting from `c90b9ce`: name the paths.**
Its gate reports `untracked source would be uploaded: agents/scratch_probe.py` and
lists every offending file. The incumbent's equivalent raises
`"Identity-eligible agent source (.py + requirements.txt) differs from committed
content"` — which names the **policy class** but not a single **member**, so an
engineer who trips it at deploy time knows *which kind* of file diverged and must
still find *which file* by hand. Same detection, strictly worse diagnostics.

**Detection really is equivalent — verified, and stronger than an earlier
description in this document.** The incumbent compares full canonical entry
**sets**, not per-file content:
`if canonical_source_entries(worktree) != canonical_source_entries(committed)`.
So additions, modifications, untracked, ignored **and deletions** all fall out of
one comparison — a deleted file is in `committed`, absent from `worktree`, sets
differ, gate fires. An earlier revision here described it as comparing "file
content, not path presence," which read literally would leave deletions
undetected. **The code is stronger than that description was.** The remaining gap
is purely diagnostic, and the fix is local: the set difference is already computed
at the raise point, so naming the diverging entries needs no new detection logic.

**What is *not* worth porting, on measurement:** that branch lists untracked files
with `git ls-files --others` deliberately **without** `--exclude-standard`, so a
gitignored `scratch.py` that remote build still uploads is caught. The insight is
right — **gitignore is not the package filter** — but the incumbent already gets
it for free: `worktree_source_entries` enumerates with `root.rglob("*")`, a
*filesystem* walk that cannot see gitignore or the index, and it compares file
**content** rather than path presence. So untracked, ignored *and* modified
tracked files all fall out of the same comparison.

**Read that with its limit, which is F1 and which both lines share.** The `rglob`
walk is filtered at `build_agent_source_tree.py` **L190** by the same
`.py`/`requirements.txt` test used for the committed view at **L172**. So "falls
out of the comparison" holds **only for files that pass that filter**. A
gitignored `scratch.py` is caught; an untracked `scratch.yaml`, or an edit to
`agents/evaluators/relevance/relevance.yaml`, is **not** — it passes the identity
check and the worktree-divergence gate and still ships. **22 of the 71 files
tracked under `agents/` fall outside the identity filter, and 12 of those are
actually packaged** — the blind spot that matters (§5). Neither line catches
them, so this remains a *parity* judgement about the untracked-file mechanism,
not a claim that the incumbent's gate is complete.

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

**`ci.yml` — I recorded this wrong, and the correction is the most consequential
defect found after the merge.** The claim above used to read that HEAD kept "the
full gate (mypy linecount, 100% coverage, suppression contract, artifact upload,
pip-audit)." **It did not.** The merge resolved `ci.yml` by taking the other side
wholesale, and every Python release gate the `coverage-release-gates` branch had
built was deleted with **no conflict marker**:

| step lost | consequence |
|---|---|
| `mypy --linecount-report coverage/python/mypy-domain` | the checker's required artifact was never produced |
| `pytest --cov … --cov-fail-under=100` | 100% line+branch enforcement gone; `coverage.json` never produced |
| `check_suppression_contract.py` | **the entire gate became dead code — no workflow invoked it** |
| `actions/upload-artifact` ×3 | no coverage/report artifacts |
| `Enforce release-gate outcomes` | the step that actually failed the job on any gate |

What replaced them was a single combined step running `ruff`, a **narrower**
hand-listed `mypy` domain, bare `pytest -q`, and `pip-audit`. Note the mypy
narrowing specifically: the checker validates the mypy domain against
`pyproject`'s `files = [packages, services, agents, scripts, tests]`, so the
hand-written CLI list was a *different* domain than the contract expects.

**Why my silent-loss scan missed it:** that scan parsed every Python module's
top-level `ast` names and compared them against each merged branch. It was
`.py`-only. `.github/workflows/` was outside its universe — and the single most
important non-Python file in the repo is the one that runs the gates. **A
completeness check is only as good as the file types it enumerates.**

**Restored** (additively, not by reverting): the four gate steps are back with
their original `continue-on-error` + aggregate-enforcement pattern, and the newer
`Bake committed Hosted Agent source identity` step and `npm run ci` consolidation
are kept. The enforce step now gates only `python_coverage` and
`suppression_contract`, because web coverage is enforced inline by `npm run ci`
(→ `test:coverage`) and the browser suite fails its own step directly; giving
those ids would have required making them `continue-on-error` too, which would
have *weakened* them.

**The restored gate was run end-to-end locally and it is red — correctly.** It
reports drift the merge introduced, which is exactly what it exists to do:
coverage source roots differ from packaging-derived roots; the coverage file set
differs from the packaging-derived source set; one file has unknown production
posture; and a list of unclassified coverage exclusions from the merged
agent-studio code (`if TYPE_CHECKING:`, `...`, `@property`) that the classifier
does not yet recognise as structural. **Fixing those is open work, not something
this integration closes** — see §5.

**`jest.config.ts` — kept the stronger gate.** `main`'s jest config set global
thresholds to 0 with per-file 100; HEAD covers `src/**/*.{ts,tsx}` broadly, and
jest coverage survived the merge inside `npm run ci`.

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

**Before touching this: the constraint that governs the fix.** The dataset owner
stated the seam, verified against the shipped code, and it is the thing most
likely to be violated by a well-meaning reconciliation:

> **A grant may only be minted by an atomic single-use consumption. Anything that
> can hand `_agent_message` a grant without one re-opens the HIGH bypass that
> `673985b` closed.**

The three surfaces and their required positions:

| function | property | position |
|---|---|---|
| `_validate_dataset_analysis` | **non-mutating** | must stay *before* `research.run`/`service.run` |
| `_consume_dataset_analysis` | **sole authority**; mints the grant | must stay *immediately before* `gateway.invoke` |
| `_require_dataset_send_grant` | structural backstop | inside `_agent_message` |

**A regression net for exactly this defect shape is preserved at `486cec7`** on
`anabil25-fix-dataset-approval-boundary` (post-integration, un-integrated, adds no
files `main` lacks): 7 tests parametrised over both execution modes. With the
whole-call-move defect simulated it turns **12 red, 7 of them those cases**. Take
it before starting, not after.

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

#### The 18 `test_dataset_approval_boundary.py` failures have a *different* cause — diagnosed and verified

An earlier revision of this document attributed them to "the approval the test
established is not being seen by the time consume runs." **That was wrong**, and
the correct cause was supplied by the dataset owner as a falsifiable prediction,
then verified here against `main`:

```
services/api/src/research_assistant_api/dataset_execution.py:14
    if inputs.get("analysis_approved") is not True:
        raise ValueError("Explicit dataset analysis approval is required.")
```

**There is a third gate, in the service layer, keyed off a client-supplied
boolean.** It is not in `app.py`, so it was never part of the nine-hunk conflict
and survived untouched. Sequence in `run_capability`: the durable gate
`_validate_dataset_analysis` (L1663) **runs first and passes**, then `research.run`
(L1674) reaches `validate_dataset_execution`, which raises `ValueError` →
`except ValueError` → **HTTP 422**. That is why the observed failure carries
foreign text, no denial reason, and no `X-Dataset-Approval-Denial`: the dataset
owner's control has **13 denial reasons mapping to 409 or 403 and none to 422**,
so a 422 is categorically not theirs.

**The tree is self-contradictory as merged.** `tests/test_v2_workbench.py:573`
(`test_client_supplied_analysis_approved_flag_grants_no_authority`) documents the
field as *"inert"*, and `studios.py:397` states a client-supplied
`analysis_approved` boolean *"is never treated as authoritative"* — while
`dataset_execution.py:14` makes that same boolean **required**. It is not inert;
it is a necessary condition.

**Severity, stated precisely — the obvious fix is wrong, but not for the reason
first given.** Adding `analysis_approved: true` to the 18 tests does **not**
immediately reopen the HIGH bypass `673985b` closed, because the durable gate runs
*first* and still has to pass; this third gate can only deny, never grant. **The
real hazard is latent:** it entrenches a client-keyed gate that *looks* like the
dataset approval control, so whoever later removes the durable gate as "redundant"
reopens the bypass instantly — and the contradicting test would still be green.

**The reconciliation question is therefore not "how do we make 18 tests pass."
It is: should this gate consult the durable server-resolved record, or be deleted
as superseded?** It must not be satisfied from the test payload.

**Independent secondary finding, true either way:** an authorization denial that
surfaces as a bare `ValueError` → 422 is **indistinguishable at the route boundary
from a malformed CSV or a bad objective**. It carries no denial reason and no
header, so it is invisible to the deploy-day monitoring built for exactly these
events. **An authorization failure must not be reported as a schema complaint** —
the same "different operational facts must stay separable" principle ruled on
three times elsewhere in this document, here appearing in the *merged* tree rather
than in either branch.

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

### Release gates — found *after* the merge, by running the restored gate

These three are merge damage, unlike everything else in this section, and are
recorded here because they are open work rather than resolved history. All three
were measured at `main`, not inferred.

- **The committed suppression contract is stale by 92 files.**
  `.github/suppression-contract.json` describes **58** production files. That was
  exactly right for the tree it was generated from (56 tracked `.py` under the
  declared roots, **0** outside the partition). Recomputed against merged `main`
  the same code yields **packaging 151 / coverage 150 / production 150 / unknown
  1**. Regenerate it once the tree settles.
  **Do not read the committed `postureUnknownFiles: 0` as evidence of anything** —
  in that snapshot `packagingFiles` and `coverageProductionFiles` are the *same*
  58-element set, so the symmetric-difference check is empty by construction. It
  is a self-consistent artifact, which is a much weaker claim than it looks.

- **`scripts/build_agent_source_tree.py` ships as a predeploy hook with zero
  coverage evidence.** It is the sole `postureUnknown` file when recomputed:
  `packaging=[azure-hook:predeploy:posix, azure-hook:predeploy:windows]`,
  `coverage=[]`. This is the *same* hook-gap class the gates work had already
  closed for `scripts/postprovision.py` and `scripts/configure_agent_rbac.py`
  (both added to coverage source and closed with behavioural tests, not
  exempted). The merge introduced a third hook script from a different branch and
  **their mechanism caught it unaided** — good evidence the partition refinement
  is sound. Worth noting that this file is the provenance producer §2 endorses as
  the release trust anchor: it ships, and it is not measured.

- **Unclassified coverage exclusions from merged agent-studio code.** The
  classifier does not yet recognise `if TYPE_CHECKING:`, bare `...` protocol
  bodies, or `@property` in the merged modules as structural, so each is reported
  as an unclassified exclusion. These are almost certainly all structural and want
  a classifier rule, not per-line suppressions.

### Dataset approval — the operator tool IS in `main` (I recorded otherwise; corrected)

**An earlier revision of this section claimed
`WorkspaceStore.dataset_approvals_without_requester_principal()` was missing from
`main` and gave coordinates to lift it from an unmerged commit. That was wrong.**
The capability is in `main`, under a different name and in a better form:

| | |
|---|---|
| `workspace.py:1207` | `dataset_approvals_blocked_by_requester_attribution()` |
| `cosmos_workspace.py:514` | Cosmos override |
| tests | `test_enumeration_narrows_to_the_genuinely_affected_set` (L1971), `test_enumeration_helper_is_scoped_to_one_project_and_under_reports` (L1118), `test_legacy_documents_omit_the_key_so_null_comparison_finds_nothing` (L1068), `test_fingerprint_bump_invalidates_more_than_the_legacy_population` (L2035) — **all four present** |

It is **narrowed** to `APPROVED` and not-yet-expired (its docstring puts the case
plainly: *"the difference between reporting '1,200 legacy approvals' and '7
approved and unexpired' — the former causes a panic, the latter supports a
decision"*), and it carries an explicit **SCOPE WARNING** that the instance is
pinned to one `(tenant, project)` pair and will **under-report fleet-wide**, with
the cross-partition query given inline. It also pins the null-shape trap: legacy
documents **omit** the key rather than storing `null`, so `= null` finds nothing
and `NOT IS_DEFINED` is the correct predicate — asserted against a real stored
document rather than argued.

**How I got it wrong, since the failure is more instructive than the entry:** I
searched for the *old name*, found zero matches, and concluded the *capability*
was absent. It had been renamed so the name matched what it returns. **That is
exactly the §7 practice — a negative search result is only as good as the
vocabulary you searched for — which I had written down and then violated.** Search
for the property, not the identifier.

**What survives, and it is the part that matters operationally:**

**Correction, measured at `main` — the spike will not say what you expect, and
this changes what an operator should search for.** On the consume path the
fingerprint gate runs *before* the attribution gate:
`consume_dataset_approval_request` (L1078) → `_check_dataset_approval_usable`
(L999), which raises `FINGERPRINT_MISMATCH` at **L1026** and only calls
`_verify_consuming_principal` at **L1044**, where `UNATTRIBUTABLE_REQUESTER` lives
(L1134). Since `_DATASET_FINGERPRINT_VERSION = 3` (L233) while the records in
question predate the version-2 bump that introduced `requested_by_principal_id`,
**every real legacy record denies with `fingerprint_mismatch` and never reaches
the attribution check.** So `UNATTRIBUTABLE_REQUESTER` is effectively unreachable
for exactly the population it was written for.

Two consequences worth acting on together:

1. **Operationally**, an operator watching for an `unattributable_requester`
   spike will see none, while `fingerprint_mismatch` — a far more alarming and
   less specific signal — spikes instead. The enumeration helper below is still
   the right triage tool; the *reason code* to expect is the surprise.
2. **For the fix**, ordering attribution before fingerprint is the change, and a
   test seeded with a matching digest **cannot detect it** — matching by
   construction opens the fingerprint gate and lets attribution fire, so the test
   passes either way. Seed a v1/v2-era digest so it stays red until the ordering
   actually changes.
3. **The affected population is strictly larger than the attribution population,
   and the helper in `main` enumerates the smaller one.**
   `_DATASET_FINGERPRINT_VERSION = 3` (L233) is part of the fingerprint
   **material** itself (L277), so **every approval whose fingerprint was stored
   under version 2 denies with `fingerprint_mismatch`** — not merely those missing
   `requesterPrincipalId`. An opaque hash makes a v2 record indistinguishable from
   a v3 record by inspection, so the surface cannot be narrowed by looking at the
   records. **`dataset_approvals_blocked_by_requester_attribution()` therefore
   under-counts by *category*, not only cross-partition** (which its docstring does
   warn about). Announce the version bump as a breaking change in its own right,
   with its own enumeration, and treat `fingerprint_mismatch` as a **third**
   monitoring signal distinct from the two attribution reasons.
   **Demonstrated by execution**, which is sharper than the reasoning: a *legacy
   but attributable* record — v2 digest, `requesterPrincipalId` present — denies
   `fingerprint_mismatch` and is **absent from the enumeration**, while a v3
   attributable record consumes cleanly. Enumerate by **fingerprint version OR
   missing requester**, never by field absence alone.

4. **A reviewer's send-outcome ordering finding is superseded in `main` — do not
   act on it.** Measured at `f574ab3` (unmerged), `dataset_send_outcome` resolved
   via `reversed(sorted(…, key=lambda item: item.recorded_at))`, so on a
   coarse clock (Windows ~15.6 ms) the sort was a no-op and the answer fell to
   list-insertion order. Their demonstration was the right shape and worth
   preserving: **the same two logical writes resolved `delivered` when issued
   sequentially and `failed` when issued concurrently** — deterministic in each
   configuration, therefore invisible to repeat-run testing, so 10/10 green proved
   nothing about it. **`main` already carries the fix**:
   `_dataset_audit_order` returns `(entry.recorded_at, entry.sequence, entry.id)`
   — the monotonic append sequence they recommended — and its docstring states the
   property directly: *"Correctness therefore rests on identity and recorded
   order, not on Python's sort being stable."* Their caution against
   `(recorded_at, id)` is also honoured: `id` is `uuid4`, so it would order ties by
   random hex *reproducibly* — a deterministic wrong answer, harder to spot than a
   flaky one — and it sits **after** `sequence`, as a final tiebreaker only for
   records predating sequencing.

**On the repeated rebuilds of this work:** **twelve** commits exist on parent
`673985b` — `1affe85` (merged), plus `aa44fd2`, `f574ab3`, `e0b5367`, `10d3e39`,
`cfb9366`, `c5fad2e`, `daf1f60`, `5238023`, `cf9064b`, `19f1625`, `9ca6432` — and
they resolve to **twelve distinct trees**, no two alike. Exactly one is merged.
Every unmerged build measured so far is *smaller* than `main` (−450 and −265 lines).
**They are indistinguishable by message and by parent; only the trees differ.**
Anything wanted from the unmerged set must be checked symbol-by-symbol against
`main` first — searching for a *name* is not enough, as the operator-tool entry
above records. **Compare trees.**

**Two enumerated-set weaknesses live in `main`, both measured, and they are the
same shape as F1's inclusion filter:**

1. **The docstring guard omits the site where the contradiction lives.**
   `app.py:1585` says `action="consumed"` **"must imply data really left"**, and
   `tests/test_dataset_approval_boundary.py:953` draws the same false converse —
   while `workspace.py` correctly says *attempted, never that a send happened*.
   The guard at `:1588` inspects exactly
   `(Entry.__doc__, WorkspaceStore.record_dataset_send_outcome.__doc__)` — **two
   docstrings, neither of them `app.py`'s.** It passes while the contradiction it
   exists to catch sits in a third site. Adding `_consume_dataset_analysis.__doc__`
   to that tuple fails in one line and forces the fix.
2. **The neutralization battery is seven hand-written anchors against 18 raise
   sites.** A reviewer applied this rule to their own method unprompted:
   `raise DatasetApprovalError` appears **18** times in `main`
   (`workspace.py` 11, `cosmos_workspace.py` 4, `app.py` 3), and the battery
   anchors **7**. It is correct for what it lists and **silent about any
   fail-closed branch nobody thought to anchor**, so a control added tomorrow is
   invisible and the report still reads *"7/7 red."* **Derivable form:** enumerate
   every `raise DatasetApprovalError(...)` from source and require each to have a
   neutralization that reddens at least one test — converting *"the controls I
   remembered"* into *"the controls that exist."*

### Runtime trust (never independently reviewed — 107 commits)

**Read this before judging the section by its heading.** "Never independently
reviewed" is true and the HMAC finding below is confirmed still live in `main`
(`infra/modules/keyvault.bicep:3`, `agent_studio/models.py:1617`). But this line
also carries the most systematic machine-checked evidence in the repo:
`tests/test_agent_studio_runtime_absence_controls.py` holds **20 numbered absence
controls**, each asserting that something is *not* present — no `default_factory`
on digest-feeding timestamps, no unconditional upsert on the head, no non-atomic
fallback in the succession retry, no `latest`/`current` accessor on the ports, no
`deployment_id` in the resolver return, no ambient `datetime.now()` reachable from
domain authorization, no hard-delete affordance on the binding writer, no secret
material in the mapping, no internal reason or `str(exc)` surfaced to clients, and
so on.

That is the §7 practice — *when a property holds because something is absent,
assert the absence* — applied more thoroughly than anywhere else in this
integration. It does not substitute for review, and it cannot: every one of those
controls asserts a property someone chose to name. But it does mean the
unreviewed surface is considerably better instrumented than the heading suggests,
and a reviewer should start by reading that file to learn which invariants are
already pinned.

**The twenty are not equally strong, and the owner graded them honestly — read
the split before relying on any one of them.** Roughly **ten are STRUCTURAL**:
member/type/`isinstance`/signature checks that **cannot be evaded without deleting
the test** — the reader Protocol declaring exactly `{get}`, `BindingResolution`
carrying no `deployment_id`, the binding writer having no delete affordance,
`RuntimeConnectionRef` holding no secret-shaped field, `RuntimeMappingView`
having no `lifecycle_state`, and the `@runtime_checkable` `isinstance` test
(`runtime_mapping_store.py:162`, with its rationale sited *at the definition*)
proving a control-plane adapter is not a reader. The other **ten are SOURCE/AST
tripwires**: no `upsert_item`, no non-atomic fallback, no `datetime.now(` in the
authz path, the mount importing no control-plane adapter, and so on. **Those catch
the harmless-looking addition but a deliberate rename would slip past.** That
residual is real, was disclosed rather than discovered, and closing it fully needs
language support that does not exist here. **Treat the source-based ten as
tripwires, not proofs.**

**Preserved but deliberately un-integrated: `c90b941` on
`anabil25-fix-runtime-trust-clean`.** That session stood down with a verified
improvement uncommitted in its worktree, which would have died with it. It is now
committed on the branch — **additive, one commit past `main`, adding no files
`main` lacks** — purely so the work is findable. It upgrades several absence
controls from source/AST tripwires (evadable by rename) to structural checks that
are not: `__protocol_attrs__` and `BindingResolution` fields asserted as **exact
sets**, the CAS `etag` parameter stripped of its default so an unconditional write
is **unrepresentable**, and the `warn_unused_ignores` guard described in §7.
**Verified before committing: 19 passed.** Take it when the absence suite is next
touched — it is the upgrade path for the ten proxies noted above.

**That file is not the complete index, though — check before concluding a
property is unguarded.** At least one absence invariant lives elsewhere: the
bidirectional no-inheritance guard between the runtime and control-plane ports is
in `tests/test_agent_studio_runtime_mapping_store.py:92-93`
(`Reader not in ControlPlane.__mro__` and the converse), not in the absence suite.
I nearly filed that property as unguarded after searching the absence file for
`issubclass`/`__bases__` and finding nothing — **a negative search result is only
as good as the vocabulary you searched for, and the guard used `__mro__` in a
different file.** Grep across the suite for the *property*, not the idiom.

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
- **F1** — **12 shipped-but-unhashed files.** *(An earlier revision of this
  document "corrected" this to 22. **That was wrong** — 22 is a different
  measurement, and the distinction matters:)*

  | measure | count | meaning |
  |---|---|---|
  | tracked under `agents/` | 71 | everything in git |
  | identity-eligible | 49 | `.py` (48) + `requirements.txt` (1) |
  | **not** identity-eligible | 22 | tracked but unhashed |
  | …of those, excluded by `.agentignore` | 10 | `*.md` ×1, `evals/` ×9 — **never packaged** |
  | **shipped-but-unhashed** | **12** | **packaged *and* unhashed ⇒ the actual gap** |

  The 12: `.agent_configs/baseline/metadata.yaml`, `.agentignore`,
  `coordinator.eval.yaml`, `dataset.eval.yaml`,
  `datasets/smoke-core/smoke-core_dg.jsonl`, and 7 files under `evaluators/`.

  **A second session later "confirmed 22" — and that confirmation is false, in an
  instructive way.** They counted *policy-excluded tracked files* (22, plus a 23rd,
  their branch's `source_manifest.json`, legitimately excluded as it cannot hash
  itself) and reported the numeral as agreement. But that is row 3 of the table
  above, not row 5: **10 of those 22 are never packaged**, so they cannot be
  shipped-and-unhashed. Re-verified at `076e757` — `agents/.agentignore` excludes
  `*.md` (1 file) and `evals/` (9 files); 22 − 10 = **12**. F1 stands at 12.

  **The trap: their number matched the figure this document had already retracted.**
  Two parties agreeing on a numeral while measuring different quantities looks
  exactly like confirmation, and here it looked like confirmation *of a known-wrong
  value* — which would have reopened a settled correction. **A confirmation that
  agrees with a retracted figure is evidence the two sides are counting different
  things, not evidence the retraction was premature.** Confirm the denominator
  before accepting the number.
  **Counting the 10 ignored files inflates the finding**; they ship nowhere, so
  their absence from identity is correct.

  The fix must be **one derived definition**: the enumerated set lives at *two
  sites* — `scripts/build_agent_source_tree.py` **L172** (committed view) and
  **L190** (worktree view) — so a new file type widens the gap **and** blinds the
  drift check in the same edit with no failing test.
  **Concretely, and worth stating because it undercuts a control praised
  elsewhere in this document:** because both views share the filter,
  `validate_worktree_matches_commit` is blind to the same files. Editing
  `agents/evaluators/relevance/relevance.yaml` in the worktree passes the identity
  check *and* the worktree-divergence gate, and still ships.
  **A test pinning the two copies equal does not close this.** Such a test
  constrains **drift between the copies**, not **the copies being jointly wrong** —
  so it buys *consistency* while *completeness* stays open. `GAP B = 0` is what
  proves the residue is a pure blind spot rather than a filter wrong in both
  directions: nothing identity-eligible is unshipped, only the reverse. **Derive
  the set from one definition; do not synchronise two.**
  **Both competing lines land on exactly the same 49-file inclusion set**, so F1
  is untouched by the walk-versus-snapshot choice and **cannot decide between
  them** — it is a *coverage* question, not a *mechanism* one. Recording the wrong
  set precisely does not make it the right set.

  **And they agree because of inheritance, not convergence — measured.** The
  filter exists verbatim at the shared merge-base
  `b7969d6:agents/shared/release.py:140`, inside the `source_bundle_digest`
  function both lines were replacing:

  ```python
  if path.suffix != ".py" and path.name != "requirements.txt":
  ```

  `b7969d6` is the merge-base of `34543f19` and `c90b9ce`. **Neither session chose
  this filter; both carried it across.** That matters for the fix: there is no
  recorded rationale to adjust, because it was never anyone's considered decision.
  **Derive the set from the packaging rule (`.agentignore`, which is what actually
  determines what ships) rather than by amending the inherited filter** — amending
  a choice nobody made tends to reproduce the same gap in a new shape. `GAP B = 0`
  is the evidence that packaging is the sound anchor: nothing identity-eligible is
  unshipped, so aligning identity *to* packaging widens with no known
  false-positive cost.
- **F-PROV** — `source_commit`/`source_tree` are recorded and never verified;
  forged values with a recomputed self-digest are accepted.
- **Wording** — `source_identity.py` and `ARCHITECTURE.md` claim an *"independently
  regenerable correctness control"*. Regenerable is true; *control* is not —
  nothing re-derives from git at runtime. Auditable, not checked. **Proven, not
  argued:**
  ```
  [STRONG]            edited manifest -> REJECTED by its self-digest
  [REPRODUCIBLE-ONLY] source file tampered AFTER build, manifest untouched
                      -> manifest still loads clean, digest=aecf01230bd324b3
  ```

  Tamper the *source* and leave the manifest alone and it loads without complaint.
  So the honest description is **reproducible identity, not attested identity** —
  and note an attacker who replaces the package replaces the manifest too and
  recomputes an unkeyed digest, which is why this is a self-description property
  rather than an attestation.

  **Exact replacement wording, agreed and ready to apply** (the claim is still live
  at `agents/shared/source_identity.py:43`):

  > The build producer derives identity from **named git objects**. The runtime
  > verifies **only** the schema and the manifest's own internal self-digest.
  > `source_commit` and `source_tree` are **reproducibility coordinates, not
  > runtime-attested claims**. A forged or post-build-tampered package requires
  > separate **signed artifact/deployment attestation** to detect.

  The fields make this concrete: `source_commit` and `source_tree` are declared as
  `Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")` (L21–22) — **shape validation,
  not identity verification.** Any well-formed hex string passes, and there is no
  `subprocess`, `rev-parse` or `ls-tree` anywhere in the runtime loader. That is
  the whole of F-PROV in two lines of code.
- **HIGH — cross-release/cross-principal replay.** Reproduced: a COMPLETED record
  replays under a successor release with no provenance check and no approval
  consumption. Latent only because every shipped descriptor defaults to
  `CompletedReplayMode.DENY`. **Latency re-verified at `main`, so the priority
  rests on measurement rather than assertion:** there are **zero**
  `completed_replay=` assignments anywhere in `agents/` or `services/`;
  `capabilities.py:124` takes the default `IdempotencyPolicy()` whose
  `completed_replay` is `DENY` (`idempotency.py:53`); and the only non-DENY values
  in the repo are two test fixtures (`test_agent_harness.py:1873`, `:5786`). So
  the vulnerable path is unreachable in shipped configuration, and **one
  non-DENY descriptor makes it live.** **The highest-value mitigation is not the
  fix** — it is an invariant test that no non-test capability sets
  `completed_replay` to a non-DENY mode. Adding the fix leaves the suite green, so
  nothing would catch the defect *or* its reintroduction.

  **Do not implement that invariant by scanning source.** A grep- or AST-shaped
  test passes for exactly the case it can see, and the things that will eventually
  break it — **a policy loaded from configuration, a capability constructed
  dynamically, a default overridden at registration** — are invisible to it. That
  reproduces the very defect class the test exists to prevent: **a control that is
  vacuously true rather than protective.** Instead **assert on the registered
  capability set at runtime**: enumerate what the harness actually registers and
  require `idempotency_policy.completed_replay == DENY` for every non-test
  capability. That binds the *property* — nothing in production replays — rather
  than the *syntax* currently expressing it.
  **Neutralization that proves it is real:** set one production capability to
  `RETURN_RESULT` and require the test to go red. If it stays green, the invariant
  is decorative.
  **Pin the definition alongside the number.** "Zero production opt-in" was
  verified three ways at once, and the counts differ by reading: **8** non-DENY
  enum *value uses* (all in `tests/test_agent_harness.py`), **5** occurrences of
  `completed_replay`, **16** references to `CompletedReplayMode`, **2**
  assignments — both in tests. **Non-test assignment to a non-DENY value: none.**
  One figure, three defensible senses; a number that carries a ruling needs its
  sense stated with it.

  **Boundary note, recorded so nobody re-derives it — and *not* a finding.**
  `argument_hash` **does** cover the arguments: both in-tree producers derive it
  from the real request (`workflows.py:237` digests
  `request.model_dump(mode="json")`; `middleware.py:496` digests
  `{"function": …, "arguments": arguments}`). A reviewer briefly suspected "new
  arguments silently ignored" and **withdrew it after measuring** — varying an
  argument changes the digest, changes the key, and yields a fresh execution, so
  the replay finding stands on **cross-release replay alone**. What *is* true is
  narrower: `capabilities.py:841` reads
  `argument_hash = cast(str, context.operation_fingerprint)`, so the capability
  layer **carries** the caller's fingerprint rather than re-deriving it.
  **`argument_hash` therefore binds what the caller *declared* the arguments were,
  not what the callee *received*** — the recorded-not-verified shape again, but
  here a caller obligation that both existing producers honour. Measured against
  the two producers that exist; it claims nothing about producers that don't.

  **Fix specification, agreed before the freeze:**
  - **Do not add `principal_id` to `IdempotencyKey`.** Principal is a *verify*
    fact, not an *identity* fact; keying on it would partition the idempotency
    space and turn a bypass into a duplicate-execution bug.
  - **Enforce actor and release provenance unconditionally across every
    disposition.** `IN_PROGRESS` and `RECONCILIATION_REQUIRED` currently raise
    deterministically and return no data, but that is a fact about today, not a
    boundary — per-disposition reasoning recreates the class the next time a
    disposition is added.
  - **Test cross-principal and cross-release separately, with distinct failure
    reasons**, so a future regression names which half broke.
  - **Reproduce acceptance *before* fixing.** A fix landed without the
    reproduction step carries no evidence that the test detects anything.
  - **Deny authority, not service.** Same principal + same release replays
    normally; a different principal is denied with a deterministic named error;
    **same principal + successor release must be decided and tested explicitly**,
    because blanket denial converts a privilege-escalation fix into a
    deployment-time outage visible only during upgrades.
  - **Note the asymmetry before starting:** `_replay_completed` never receives the
    invocation context, so the release check is ~5 lines with both values in hand
    while the principal check needs a signature change. That fork is *why* the
    omission exists — the correctly-guarded path bundles both checks in a function
    that does have the context.

### Dataset — **NO REVIEW COVERS THE SHIPPED TREE.** Read this before citing an approval

**The "APPROVED, 2 LOW" verdict does not apply to what merged.** The reviewer
approved `c5fad2e`, `cfb9366` and `10d3e39`. What shipped is `1affe85`, and
**all three reviewed `services` subtrees differ from it.** Measured:

```
git diff --stat 10d3e39 1affe85 -- services packages
  cosmos_workspace.py    26 ++
  workspace.py          148 ++++++++++++++
  2 files changed, 168 insertions(+), 6 deletions(-)
```

**168 lines of production source landed after the last review, concentrated in
`workspace.py` — the file carrying the consume-path authorization logic.** The
reviewer detected this themselves by subtree-hash comparison and reported it
rather than letting the approval be cited: *"no approval of mine covers the
shipped dataset tree."*

**This is the failure this whole document warns about, landed on this document.**
A verdict was recorded against a range and then cited against a tip that had moved
— exactly what §7 says to guard against. The findings below are therefore **known
and open**, not "resolved because it was approved," and the HIGH CSV-egress bypass
that `673985b` closed **is** genuinely closed (verified by exhaustive egress
enumeration) — that one does not depend on the later delta.

- **Ordering + fixture, one work item.** `workspace.py` checks `plan_fingerprint`
  before `_verify_consuming_principal`, so `UNATTRIBUTABLE_REQUESTER` can never
  fire for the legacy population it exists for. **A test masks it** —
  `test_unattributable_requester_is_observable_on_the_wire` builds a v3 fingerprint
  then pops the requester, a state that cannot occur in production. Fix the
  ordering **and** seed the test with a v2-era digest.
  **Operational consequence:** the deploy-day spike surfaces as
  `fingerprint_mismatch`, which is **indistinguishable from a plan-swap attempt** —
  so the signal that should say "expected migration" says "possible attack."
- **Documentation contradiction.** Correct wording was *added* without the incorrect
  wording being *deleted*; `app.py` still says `action="consumed"` "must imply data
  really left". The doc-assertion test inspects two docstrings and misses the third
  — adding `_consume_dataset_analysis.__doc__` to the set fixes it in one line and
  forces the correction.
- **LOW** — a failing outcome-write inside the `except` handlers masks the original
  error; a *successful* send can be reported as 500.
  **Confirmed against the shipped code, and the correct fix is not the obvious
  one.** In **both** routes' `except` ladders, `_record_dataset_send_outcome(...)`
  runs **before** the `raise`, so if the audit append throws — a Cosmos error, say
  — it propagates and **displaces the in-flight exception**: the caller sees the
  audit error rather than the 502/503 that actually occurred, **and the
  send-outcome entry is lost anyway.** Both halves fail together.
  **A bare `try/except: pass` is the wrong remedy** — suppressing the audit failure
  recreates the lossy-audit defect this very entry exists to prevent. The write
  must be guarded so it cannot displace the in-flight exception **while its own
  failure still surfaces by some other route**. That is a deliberate design
  decision, not a suppression.

### Provider (APPROVED ×3) — **the carried item is CLOSED and merged**

**Third approval is the tag-range review of `f96e904..f36947b`** (9 commits, 0
merges, linear), pinned from `review/provider-candidate-1` rather than a reported
SHA. It raised two non-blocking findings, and **one of them is already superseded
by a commit that is in `main`** — recorded here so the verdict is not read as
describing the current tree:

- **Superseded — empty-string behaviour on `discovered_provider_version`.** At
  `f36947b` a present-but-empty `""` failed closed and the whole instance was
  rejected. **`9eb4950` changed that deliberately** (see below): `""` now
  normalises to `None`. The reviewer's observation was correct at the tag and is
  moot at `main`.
- **Stands — scope of `b45f014`.** A repo-wide `* text=auto eol=lf` landed inside
  a provider-adapter candidate, and its side effect **blinded one of this
  branch's own controls**: with LF forced in the working tree, a defeated golden
  exemption produces no CRLF and no pin movement, so the byte-level checks cannot
  see it. A compensating test was added at `f36947b` and is correct — but the
  *need* for it was manufactured by an out-of-scope change. **No live harm**
  (zero stored-CRLF text files), so this is a process finding.
  Worth knowing: `.gitattributes` **already documents this itself**, stating that
  the compensating test "is the ONLY detector" and that the ordering is
  load-bearing and machine-enforced. The control and its own limitation are
  recorded in the same place, which is the right pattern.

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

**Known migration consequence of that fix — the pin moves, and the pin is
drift-enforced.** `discovered_provider_version` sits **inside**
`compute_instance_fingerprint`'s payload (`capability_registry.py:168`), so
normalising `"   "` → `None` is **not digest-neutral**. Measured:

```
fingerprint(dpv='   ') = sha256:65761615d1347666…
fingerprint(dpv=None)  = sha256:6e0416b4c1e1858d…
identical: False
```

That fingerprint is enforced, not merely recorded — `capability_registry.py:919`
raises *"(instance_ref.fingerprint mismatch) — rebind and re-review before
release/invoke"*, and `models.py:453` states it **hard-fails on drift (stale
binding)**. So an **already-released binding** pinning an instance whose provider
reports a whitespace-only version will recompute a different fingerprint and hard
-fail as stale, requiring rebind and re-review.

**This is correct behaviour and the fix should stand: the pin genuinely changed.**
Scope is narrow — only blank-but-present versions, i.e. **the population that
breaks is exactly the population that was wrong.** Same shape as a golden
re-baseline: a correctness fix that necessarily moves a pin should move it
*deliberately and audibly*, never be avoided to preserve a wrong pin. Land it as a
stated migration consequence rather than letting it surface at release time.

**One item IS outstanding, found after approval and verified in `main`: aggregate
warning volume is unbounded.** Every individual axis in
`capability_discovery.py` is bounded — `DEFAULT_MAX_RESPONSE_BYTES = 8_000_000`,
`DEFAULT_MAX_PROVIDERS = 250`, `DEFAULT_MAX_DESCRIPTORS_PER_PROVIDER = 500`,
`DEFAULT_MAX_INSTANCES_PER_PROVIDER = 2_000`,
`DEFAULT_MAX_OPERATIONS_PER_DESCRIPTOR = 200`, `DEFAULT_MAX_CONCURRENCY = 8`
(L84–L89). **No bound covers their product.**

Two structural reasons, both read from the code:

1. **`max_response_bytes` bounds each response in isolation.** It is enforced per
   call at L1087, inside the fetch, so it cannot see the accumulation.
2. **Retention is simultaneous, not streamed.** `asyncio.gather(...,
   return_exceptions=True)` (L1234–1236) holds every provider outcome at once;
   the `asyncio.Semaphore(self._max_concurrency)` at L1228 bounds in-flight
   *requests*, not retained *results*. So the ceiling is **concurrent peak
   memory**, not a sum over time.

Measured by the owner and independently by the reviewer, consistent with the
constants: aggregate scales linearly in **responses**, not providers, because the
catalog fetch (`_get_json("v1/providers")`, L1124) is a response in its own right
and its warnings are concatenated with the per-provider ones (L1224, L1239). So
the relation is **`(1 catalog + N providers) × per-response warning bytes`**,
giving a ceiling at shipped defaults of **`(1 + 250) × 8 MB ≈ 2.01 GB`** — an
amplification of **251×**, measured across 1/5/10/25 providers with no inflection.
A second datum: 150 KB of warning text under a 200 KB cap emitted **324 warnings /
295,650 bytes**, passed through verbatim.

**Mind the seam in that figure: the *shape* of the bound is measured, the
*magnitude* is arithmetic.** Linear scaling was measured at small N (up to 25
providers, and independently at N=6 where 960,000 chars `== 6 × 4 × 40,000`
exactly). The ~2 GB is **extrapolated to the shipped default of 250, not
observed there** — at that scale allocator, GC and OOM-killer behaviour could
dominate long before the arithmetic ceiling is reached. The structural claim
(nothing bounds the aggregate) is established; the specific number is a
projection. **Do not cite ~2 GB as a measured figure**, and if the decision turns
on the magnitude rather than the existence of the gap, measure at N=250 first.

**Do not record this as a mislabelled control — the setting is honest, and the
ingress bound is well implemented.** `config.py:257` reads *"Hard cap on the
number of bytes read from **any single** provider discovery HTTP response,"*
which is exactly what it does. Verified at the mechanism: `_get_json` streams via
`response.aiter_bytes()` and the size check at **L1087 fires *before*
`chunks.append(chunk)` at L1091**, so an over-cap chunk is **never buffered** and
`json.loads` (L1098) only ever sees a bounded buffer. A hostile provider cannot
force a parse or an allocation beyond 8 MB on any single response.
**Ingress protection works; there is simply no second bound on the aggregate.**

**So this is a *circumstance*, not a control failure — the distinction both
reviewers converged on independently.** The egress bound is **arithmetic, not
enforced**: nothing stops a provider spending its full per-response budget on
warnings across every response. Measured across 1/5/10/25 providers, emitted
bytes per response held at 40,150 · 40,106 · 40,096 · 40,089 — **linear, no
inflection, nothing intervening** — and the cardinality family confirms the
absence structurally (`a warnings-specific cap present: False`).

**It is not an amplification finding.** Reaching it needs a provider already
Entra-authenticated and already permitted 8 MB per response, and it **fails in
the honest direction — a large snapshot, not a wrong one.** Whether ~2 GB of
aggregate is acceptable is a judgement about the deployment, not about the code.

**Cheapest correct disposition is a sited rationale, not necessarily code.** State
beside the setting (or in the class docstring) that the byte cap is per-response
and that aggregate warning volume is bounded transitively at
`MAX_RESPONSE_BYTES × (1 + MAX_PROVIDERS)`. That converts an inference a reader
must make into a stated bound they can check — **and if that figure is judged
acceptable, saying so *is* the fix.** If a structural bound is preferred instead,
the module's own pattern supplies the shape: a `max_warnings_per_response`
cardinality cap alongside the other four, plus a `max_length` on message text
matching the `principal`/`correlation_id` precedent.

**The sharpest form of it:** this module *already applies* the fix pattern.
`principal` and `correlation_id` carry explicit `max_length=200` (L108–L109). The
one string field with no length bound is `_wire_warnings` (L604) — **the field
that accumulates.**

**Note the convergence:** `_wire_warnings` now carries *two* structural findings —
no length bound here, and no escaping at the boundary (the log-injection item).
Both are on the diagnostic channel, which is the surface nobody models as attack
surface. That is the second time that category has produced a finding in this
program.

**A third member of the same family, measured and deliberately *not* escalated:**
`unavailable_reason` is a raw, unvalidated `payload.get(...)` at
`capability_discovery.py:894`. A reviewer proved by execution that it is
**display-only** — it is not among the nine `ProviderInstancePins` fields, and two
instances differing *only* in `unavailable_reason` produce an **identical**
`config_fingerprint`. So it never reaches a pin or a digest. Worth siting; not
worth blocking. **Recorded because the negative result is the useful part:** the
same sweep that found it also confirmed every digest and pin input now routes
through `_verbatim_required_digest` / `_verbatim_required_text` /
`_verbatim_optional_text`, so exactly **two** unvalidated wire reads remain in the
module and **both are on the display path**.

This is an **enumerated-axes failure**: each bound is real and correct, and the
gap is that no one asked what the product of two bounded axes could reach.
Remediation is a per-warning `max_length` plus an aggregate cap, not a new axis.

Nothing else outstanding on this workstream.

### State / web

- **A post-fix flake measurement at `202a957` is superseded — do not read it as
  describing `main`.** A reviewer measured the tip at `202a957` and reported that
  the `delay: null` fix had been applied to `studio-components` but **not** to
  `research-workbench`, which then hard-timed-out (1 run in 10,
  `research-workbench.test.tsx:1287`, "Exceeded timeout of 5000 ms"). Their
  conclusion — *the flake was not fixed, it moved* — was **correct for that
  tree**. Measured:

  | `research-workbench.test.tsx` | bare `userEvent.setup()` |
  |---|---|
  | at `202a957` | **22** |
  | at `main` | **0** |

  `c6370e5` ("remove the remaining bare `userEvent.setup()` delay") landed after
  their measurement and **is merged**. Their recommendation — extend the fix to
  `research-workbench` — is already implemented.
  One loose end they raised does *not* apply: `research-markdown.integration.test.tsx`
  contains **zero** `userEvent` references, so whatever drives its ratio, it is
  not the setup delay.

- **The `userEvent.setup()` conversion is now complete for the state lineage** —
  `c6370e5` landed after the first integration pass and is merged. **50 bare calls
  remain in `main`. 49 belong to the agent-studio workstream, which never did this
  conversion at all; the 50th does not:**

  ```
  21  agent-workspace.test.tsx      agent-studio
  15  agent-registry.test.tsx       agent-studio
  13  connections-view.test.tsx     agent-studio
   1  error.test.tsx                close-web-coverage  <- NOT agent-studio
  ```

  `userEvent.setup()` defaults to `delay: 0`, which awaits a real
  `setTimeout(…, 0)` between **every** dispatched event; a test with ~25
  interactions accumulates 100+ hops that cost real time under a loaded event
  loop. Converting to `delay: null` preserves every event and its ordering.
  **Verify per file before converting** — no `setTimeout`/`setInterval`/
  `requestAnimationFrame` and no debounce in the component under test — rather
  than assuming the state lineage's result generalises.
- **The fix was applied to the suite that was not at risk** — *measured at
  `202a957`; see the superseded note at the top of this section, `main` has since
  converted all 22 sites.* `studio-components` carries a `15000` override on its
  heaviest test (ratio 0.34). `research-workbench`
  has **no override**, sits at **0.735**, retained all 22 unconverted sites at that
  tip, and is
  the historical failing site. `research-markdown.integration` is at **0.523** and is
  in neither the fix nor the report.
  **The denominators here were themselves wrong once and are worth trusting only
  as corrected:** an initial pass reported *zero* per-test overrides, which put
  `studio-components` at 5131/5000 = **1.026** and made it look like the suite at
  the edge. Ground truth is **five** overrides — `15000` at
  `studio-components.test.tsx:3814` plus `10000` ×2 there and ×2 in
  `workspace-views.test.tsx` — verified independently in `main`. Correct ratio
  0.342, and the real edge is `research-workbench` at 0.735 with a genuine default
  budget. **Within one suite the longest test is not the closest-to-budget test
  when budgets differ.**
- **70 viewport triples are reported but deliberately not gated, and the refusal is
  correct.** A blanket `viewports: ALL_VIEWPORTS` on all 77 interactions had been
  *positively asserting* that all 298 states were proven at desktop, tablet and
  mobile, while tablet and mobile actually proved three each — and the test
  guarding it asserted that same constant back for every interaction, so **it
  verified nothing.** Replaced with scoping derived from the app's real media
  queries (`max-width: 1180px` turns the evidence inspector into a scrimmed
  overlay; `max-width: 900px` turns the rail into a drawer with a control that
  does not exist at desktop), giving `requiredViewportStateCount: 368` alongside
  the unchanged flat `298`.

  **Gating the remaining 70 would manufacture false credit.** The specs covering
  those shell states *do* exercise the breakpoints — but via explicit
  `page.setViewportSize(...)` inside the test bodies, under the desktop project.
  Re-running them under tablet/mobile projects proves nothing, because **the test
  discards the project's viewport on its first line.** Making them gateable
  requires those tests to stop hard-coding viewports and take them from the
  project — a real refactor, correctly not smuggled into a fix commit.
- **Machine-wide port locks.** `tmpdir()/research-assistant-playwright-port-locks`
  with fixed ports 40105/40106 and no per-checkout namespace; confirmed by finding a
  lock file from another checkout mid-run. **Will break parallel CI shards.** Fix is
  a per-checkout namespace or ephemeral port — *not* a longer timeout or retry, which
  converts a deterministic collision into a slow flake.

  **And the obvious isolation fix does not work — verified in-tree, so nobody
  spends a cycle discovering it.** The natural move is to isolate the lock
  directory with `mkdtempSync`, as `port-lock.test.ts` already does. It cannot
  work for `port-lock-handshake.test.ts`, because `globalSetup` calls
  `defaultPortLockDeps()` **with no argument** at `port-lock-handshake.ts:49` and
  exposes no injection point:

  ```ts
  // port-lock-handshake.ts:49
  const deps = defaultPortLockDeps();

  // port-lock.ts
  export function defaultPortLockDeps(
    lockDir: string = join(tmpdir(), "research-assistant-playwright-port-locks"),
  ): PortLockDeps
  ```

  **The test cannot isolate itself without ceasing to exercise the real path** —
  isolation and fidelity are in direct conflict here, which is why the cheap fix
  is not merely suboptimal but wrong. Since `defaultPortLockDeps` *already* accepts
  `lockDir`, the minimal real fix is an env-var override (e.g.
  `PLAYWRIGHT_PORT_LOCK_DIR`) read by `globalSetup` and `playwright.config.ts`,
  defaulting to today's shared path — after which a per-shard namespace is one
  variable. **Scope: `port-lock-handshake.test.ts` only** (hardcoded 40101–40106 on
  the shared dir); `port-lock.test.ts` already uses per-run `mkdtempSync` and is
  safe.

### Coverage / suppression gates

**If you implement the packaging + coverage cross-check, normalize first.** The
rule is *derive the production set from both the packaging manifest and the
coverage roots, and fail `unknown` where they disagree.* Tested against this tree,
it needs one companion clause or it fires on correct code immediately:

- **Shape disagreement is noise.** Packaging names **distribution** roots
  (`services/api`); coverage names **import** roots
  (`services/api/src/research_assistant_api`). As literal strings the two sets are
  **disjoint**, so an unnormalized comparison marks *everything* unknown.
  **Normalize distribution root → `src/<package>` before comparing.**
- **Scope disagreement is signal — keep it.** Coverage enumerates `agents/`
  subdirectories individually (ten entries) while `azure.yaml` ships `./agents` as
  a whole tree, across nine `azure.ai.agent` services. So a **new** `agents/<x>/`
  is shipped-by-packaging and not-measured-by-coverage until someone adds it —
  exactly the enumerated-domain shrink the exact-set assertion exists to catch.
  **Packaging's whole-tree grant is what catches coverage's enumeration going
  stale**, which is the cross-check earning its keep.

**A guard that cries wolf once gets a suppression the second time and is deleted
the third.** Ship the normalization with the rule, not after it.

- **Growth requires `--suppression-addition-reason` — but only on the
  `--write-baseline` path, and CI does not use it. Measured.** The guard is gated
  at `check_suppression_contract.py:1052` (`if args.write_baseline:`), while
  `ci.yml` invokes the checker **bare**:
  `run: uv run python scripts/check_suppression_contract.py`. **So the growth
  guard never executes in CI** — only the exact-set verify does, and a baseline
  edited by hand and committed reaches CI as `HEAD`, which the verify compares
  against source rather than against its own history.
  **Worse, the review record is not protected by verify.** `validate_inventory`
  iterates `record["additionReviews"]` (L916) and validates the *shape of entries
  present* — dict, non-empty reason, int count — but never requires an entry to
  **exist**. Measured by stripping every entry and re-running:
  `errors 0 → 0, new errors caused by stripping: 0`. **The entire human-review
  record can be deleted by hand and the gate stays green.** A second datum from
  the same run: there are currently **zero** `additionReviews` entries, so the
  mechanism is inert as well as unverified.
  **This makes `baselineReviewPolicy`'s own claim — *"No semantic laundering"* —
  an assertion the tool does not check.** Note the irony recorded elsewhere in
  this document: that policy string *is* pinned by exact comparison, so the
  sentence cannot be softened, but the property it names is unenforced. **A
  protected claim about an unprotected property.**
  Remedy is one of: verify `additionReviews` in the **verify** path, or restore the
  parent-commit comparison **with `HEAD^`** — the argument matters, because in CI
  `HEAD` *is* the PR commit including any hand-edited baseline, so a `HEAD`
  comparison would catch only uncommitted edits, which never occur in CI.
  **Shrink is still unguarded** — correctly deferred until `role: load-bearing`
  entries exist.
- **Re-measured at `main`: the coverage gate fails by 277 statements / 112
  branches, not 54 / 18.** The original figure was correct for the tree it was
  taken on, and its *reasoning* still holds — the nine `main.py` entry points at
  6 statements each are still there, and **eight of the nine still miss all six**
  (`agents/coordinator/main.py` is now covered), giving **48 statements / 16
  branches**. What changed is the other **229 statements / 96 branches**, which
  come from code merged from branches the gates work never saw. Overall: 17,448
  statements, 3,950 branches, **98.18%**.
  **Read the 229 with its caveat:** 71 tests are failing in `main`, so an unknown
  share of it is failing-test artifact rather than genuinely untested code. The
  48/16 entry-point figure is the trustworthy part; re-measure the remainder once
  the suite is green.
  **Decision unchanged: test the entry points, do not exclude them** — a
  six-statement entry point no test imports is exactly where a wiring defect
  hides, and harness N1 is that exact defect.
  **But specify them as *entry-point behaviour* tests, not import tests.** An
  import-plus-factory-wiring smoke test would take those nine files from 54
  uncovered statements to 0, turn the gate green, **and not catch N1** — because
  N1 is not "the module fails to import," it is **a deleted call on a line a smoke
  test happily executes.** Measured: with the call removed the suite was
  100/100 green, predeploy exited 0, the manifest was written and the runtime
  accepted drifted bytes. For each `main.py`, assert what the entry point must
  *install or invoke*, then **neutralize it — delete the call and require red.**
  **If deleting a call from `main()` leaves the new test green, the test bought
  coverage and no protection**, and excluding the file would have been the more
  honest option. The exclusion decision and the test-quality decision are
  separable, and only the first was ever in question.

---

## 6. Environment

- A remote named `Main` collides case-insensitively with branch `main` on Windows,
  so `git rev-parse main` warns about ambiguity and `git worktree add` from `main`
  fails. Fix: `git remote rename Main origin`.
- `uv sync --all-packages --all-extras` is required; a plain `uv sync` leaves
  `fastapi` and the workspace packages uninstalled.
- **Six review tags exist and five are reachable from `main`.** They are the only
  references in this program that still point where they did when their verdicts
  were written — branch tips moved repeatedly, tags did not.

  ```
  review/dataset-candidate-1   10d3e39   NOT in main   <- see below
  review/gates-candidate-1     a781298   in main
  review/harness-candidate-1   34543f1   in main
  review/provider-candidate-1  f36947b   in main
  review/state-baseline-1      075057f   in main
  review/state-fixed-1         4fe0ce6   in main
  ```

  **`review/dataset-candidate-1` being unreachable from `main` is not a defect —
  it is the durable proof of the §5 approval-coverage gap.** It pins the tree that
  was actually reviewed, so the claim "no approval covers the shipped dataset tree"
  stays checkable by anyone, indefinitely, with
  `git rev-parse review/dataset-candidate-1:services` against `main`. **Do not
  delete these tags.** Without them the verdicts become unfalsifiable prose.

- **The entire integration is local-only.** `Main/main` is at `a62cd0a`; local
  `main` is **289 commits ahead** and nothing has been pushed at any point. The
  "one resolved branch" state described throughout this document exists **on one
  machine**. Publishing it is a deliberate, unmade decision — recorded here because
  a reader would otherwise reasonably assume the merge is shared.

- **Two stashes existed and were invisible to every check used in this wind-down.**
  Recorded because `git stash` entries appear in neither `git log` nor
  `git status`, are unreachable from any branch, and **survive branch deletion** —
  so a "clean tree, one branch" verification passes with them present. Both were
  found by an outside session, not by any audit here.

  ```
  stash@{0}  On main   "pre-integration: local AGENTS.md edit"   -- RESOLVED
             apps/web/AGENTS.md   +3 −1
             Documented that node_modules/next/dist/docs/ resolves from the file's
             directory (in a monorepo `next` may be invisible from the repo root),
             and that the block is re-added by `next dev` via
             node_modules/next/dist/server/lib/generate-agent-files.js — so
             dropping it from a diff only recreates the uncommitted change.
             POPPED at 8e488cd and it applied as a NO-OP: all three lines were
             already committed at 9101a2f (present at AGENTS.md L4 and L6). The
             stash had become redundant during the integration and nobody noticed,
             because applying it changes nothing. Stash object 983909957 remains
             in the object store if the history is ever wanted.

  stash@{1}  On anabil25-workflow-page-redesign  "abandoned-workflow-redesign"
             apps/web/src/app/globals.css                   +965
             apps/web/src/components/studio-components.tsx  ~730
             1377 insertions, 318 deletions
             The workflow-page redesign the user halted after two turns. Never
             committed, by instruction. RETAINED by explicit decision — kept
             stashed, not applied and not dropped.
  ```

  **Nothing is lost and nothing is live.** The retained stash is deliberately
  abandoned — but *abandoned* and *forgotten* are the same state unless someone
  writes it down, which is the only reason this entry exists. **And note the
  second-order trap the resolved one demonstrates: a stash can quietly become a
  no-op**, so "it applied cleanly" is not evidence it carried anything. Check the
  resulting diff, not the exit code.

## 7. Practices worth keeping

From the review program that produced §5.

- **Make an unnecessary suppression a build failure at the edit site — the
  strongest form of "assert the absence."** With `warn_unused_ignores = true`
  (`pyproject.toml:83` here), write a `TYPE_CHECKING`-only assignment that *should*
  fail type-checking and mark it `# type: ignore[assignment]` — e.g. assigning a
  control-plane store to a runtime-reader port. **If someone later adds the
  convenience method that makes the assignment valid, the ignore becomes unused
  and mypy fails — at the line they edited**, not in a distant test they may not
  run. This beats a source scan on the axis that matters: **it cannot be evaded by
  renaming**, because the type checker resolves structure rather than text. It is
  the natural upgrade path for the source/AST tripwires in the absence-control
  suite, roughly half of which currently catch only the obvious edit.
  *(Related upgrades from the same line, recorded because the technique
  generalises: assert a Protocol's `__protocol_attrs__` as an **exact set** rather
  than checking for known-bad members, so any addition fires; and remove a default
  from a CAS parameter — `_replace_head_or_claim_error(etag=…)` — so an
  unconditional write is **unrepresentable** rather than merely untested.)*
- **Overstating a control's burden is its own route to its removal.** A team
  documenting a fail-closed release-identity check nearly wrote that every
  developer must generate a manifest before first run. They corrected it: the
  manifest is a **tracked** file, so an ordinary checkout already has a valid one,
  and local source changes make it **stale, not absent** — the fail-closed path
  bites on *missing or corrupt*, which is rarer and less alarming. **The reason
  they gave for caring is the transferable part:** overstating the requirement
  *"would have made the control look more burdensome than it is, which is its own
  route to someone adding a fallback."* Understating a gap gets a control trusted
  past its range; **overstating its cost gets the control deleted by someone
  optimising a workflow.** Both are accuracy failures, and only the first is
  usually treated as one.
  *(The tracked-vs-generated distinction matters here and is the §2 design split:
  a checked-in manifest can go stale and needs a freshness check; `main`'s
  `.release/source-tree.json` is build-produced and untracked, so it cannot go
  stale but must be produced — different burden, different failure, same need to
  describe it exactly.)*
- **A test written to close a coverage gap must fail under mutation of the thing
  it now covers — otherwise the gate was satisfied rather than the risk.** This is
  the sharp edge of *neutralization over coverage*: closing a gap **to reach a
  threshold** selects for the cheapest test that executes the lines, and the
  cheapest test is almost always an import or a construction. Measured on this
  repo: nine `main.py` entry points at 54 uncovered statements would go to **0**
  under an import-plus-wiring smoke test, turning the gate green while leaving N1
  — a **deleted call on a line the smoke test executes** — entirely undetected.
  **Coverage-driven tests are the population most likely to be decorative,
  precisely because a number, not a risk, motivated them.**

  **Here the cheapest test is not merely useless, it is harmful — and the reason
  is structural.** All nine entry points are identical and six statements long:

  ```python
  import sys
  from pathlib import Path

  sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

  from coordinator.factory import run     # resolves ONLY after line 4

  if __name__ == "__main__":
      run()
  ```

  Verified in-tree: **9 of 9 mutate `sys.path`**, 9 × 6 = 54, and no test imports
  any of them (`test_connector_adapter_entrypoint.py` imports
  `research_assistant_connector_adapter.main`, a different module). Two
  consequences an implementer must design around:

  1. **They are not importable as `agents.<name>.main`.** The line-6 import is
     `coordinator.factory`, reachable only through the path hack — so the obvious
     `import agents.coordinator.main` does not work.
  2. **Importing one mutates global interpreter state that persists.**
     `sys.path.insert(0, …)` outlives the test and can change how *later* tests
     resolve modules — order-dependent pollution, the exact class flagged on the
     provider branch.

  **So the naive nine-smoke-test remedy would satisfy `fail_under = 100`, leave
  N1-class wiring defects uncaught, *and* introduce cross-test pollution — worse
  than the gap it closes.** The specification is: exercise the entry point without
  leaking `sys.path`, assert *what it invokes*, and verify the test fails when that
  invocation is removed. `tests/test_connector_adapter_entrypoint.py` is already a
  worked example of that shape in this repo — it imports an entrypoint module and
  asserts bounded network defaults under `monkeypatch` — and is the template to
  copy rather than inventing one.

  **The general form: a gap's size tells you nothing about the shape of its fix,
  and a remedy specified from the number alone will be the cheapest thing that
  moves the number.** 54 sounds like nine trivial tests; it is nine tests that each
  need isolation, behaviour assertion, and a neutralization check.
- **Reading does not produce random error — it produces error biased toward the
  story you already hold.** Every read-based figure corrected in this program was
  wrong in the **alarming** direction: 22 shipped-but-unhashed files instead of
  12; an inflated suppression census; a merge assumed uniformly damaging that
  turned out to be one gate broken and another healed. None landed on the
  reassuring side. **That asymmetry is the argument for measuring even when the
  reading seems obviously right — especially then**, because a reading that
  confirms the current story is the one least likely to be checked.
- **Crossed messages manufacture plausible causal stories — prefer "these arrived
  out of order" over "someone acted."** Three attributions in this program were
  made by inference from *timing* rather than from the record, and all three were
  wrong in the same direction: they credited an actor where the truth was two
  things simply crossing. In one case the flattering-to-the-other-party version —
  *"a reviewer overrode my instruction"* — was inverted; the artifact had been
  produced **before** the instruction, and what actually happened was that **an
  author reversed a decision after reading evidence already in flight.** In a
  high-latency multi-party loop the causal story is the least reliable part of any
  reconstruction, and it is the part nobody checks because it is not a claim about
  code. **Attribute from the record or not at all.**
- **"Two parties agreed" is not evidence until you check whether they could have
  inherited the agreement.** This document contains two convergence cases that
  look identical and conclude oppositely, so the discriminator matters more than
  either instance:

  | case | relationship | reading |
  |---|---|---|
  | two sessions independently computed **22** shipped-but-unhashed files, by *unrelated* tool errors (`check-ignore -X`; a PowerShell array-property count) | **no shared ancestor** | genuine convergence → **the error is natural**, and worth guarding against |
  | two competing lines share the `.py` + `requirements.txt` inclusion filter | **shared base `b7969d6`** — the filter exists verbatim in `source_bundle_digest()` there | **inheritance, not convergence** → **the policy is unexamined**, and neither line chose it |

  The second looked like independent corroboration and was the opposite: a
  **shared blind spot reading as independent confirmation**. It survived every
  review because it was never in a diff. **Check for a common ancestor before
  treating agreement as corroboration** — otherwise the strongest-looking evidence
  is produced by the weakest mechanism.
- **Neutralize in both directions — they answer different questions.** Mutating
  *away* from correct asks **"is this guarded?"**; mutating *toward* correct — a
  reverse neutralization, i.e. applying the fix and seeing what breaks — asks
  **"is this defect load-bearing?"** Only the second tells a remediation planner
  whether the fix will rewrite existing tests, and it **cannot be answered by
  reading**, because a test that depends on defective behaviour looks exactly like
  a test that documents correct behaviour. **Green in both directions means the fix
  lands clean *and* nothing catches its reintroduction** — which is the pair of
  facts you need before scheduling the work, and neither alone supplies it.
- **Under-quantified is not false — and it needs a different remedy.** Two durable
  sentences in this repo were *"true along the path the author was reading, and
  silent about the path they weren't"*: **"an independently regenerable
  correctness control"** (true for an auditor holding the repo, false at runtime)
  and **"prior-release approvals are rejected"** (true on `ACQUIRED`, silent on
  `COMPLETED`). Neither is a false statement; both are **under-quantified**. A
  reviewer checking either against the path it describes finds it correct, which
  is why they survive. **The remedy is not to weaken the claim but to state the
  negative explicitly**, and this repo already contains the model sentence for
  how: *"It never hashes worktree, ZIP, or OCI bytes."* Write the missing
  quantifier in that voice.
- **Match the check's scope to the proposition's scope — a file-level answer to a
  field-level question is wrong in *either* direction.** Asked whether idempotency
  lookup identity is release-independent, `grep release agents/shared/idempotency.py`
  returns **4 hits** and reads as *release-dependent*. It isn't:
  `IdempotencyKey` has **7 fields — `tenant_id`, `project_id`, `binding_digest`,
  `operation_id`, `destination`, `caller_key`, `argument_hash` — and none is
  release-related**. The hits live in `IdempotencyApprovalProvenance` and in
  function signatures, i.e. **two different models in the same file**, which is
  exactly the distinction the claim turns on.
  **Note the direction, because it is the opposite of every other entry here.**
  The other loose checks in this document produced **false negatives** — clean
  when they shouldn't have been. This one produces a **false positive**: it would
  have *contradicted a true claim* and manufactured a finding. **A check too coarse
  for its proposition is not biased toward safety; it is biased toward noise in
  whichever direction the coarseness happens to fall.**
- **Satisfying the letter of a fix can still break the thing the fix protects.**
  A defective audit ordering resolved ties by list position, so the stated
  requirement was *"do not rely on sort stability."* An `id` tiebreaker satisfies
  that literally — `uuid4` is deterministic — and is **causally wrong**: with a
  shared timestamp it can order `consumed` before `decided`, which for an audit
  trail is a worse failure than the one being fixed. The correct key is
  `(recorded_at, sequence, id)`, where a **persisted monotonic sequence** carries
  causal order and `id` only makes the order total for records predating
  sequencing (`cosmos_workspace.py:247-250` continues the counter past the maximum
  persisted value on cold load, so a replica cannot reuse one and invert a trail).
  **The test that caught it asked what the order *means*, not whether it was
  deterministic** — check a fix against its *purpose*, not against the sentence
  that specified it.
- **Choose operation order by which crash-intermediate state fails closed.** When
  a logical operation spans two writes, the ordering is not a style question — it
  decides what a crash between them leaves behind. Worked example from the runtime
  line: **GRANT** claims the head *then* creates the binding, because a crash
  leaves an inert dangling claim and an absent binding denies; **REVOKE**
  tombstones the binding *then* clears the head claim, because the reverse opens a
  window where the head is free while the old binding is still ACTIVE — a new
  client could be granted while the old one still has access, i.e. **two live
  bindings at once**. Both orderings were chosen by asking *what does the
  half-finished state permit?*, not by symmetry.
  **The dividend is that one recovery path covers both crashes:** a reconciler
  that clears a dangling claim **only after verifying no ACTIVE binding names the
  client** handles a crashed grant (binding absent) and a half-finished revoke
  (binding revoked) identically, and can never free a legitimately-held head.
  Ordering for fail-closed *and* convergence on a single intermediate state is
  what makes recovery one function instead of a case analysis.
  **Two corollaries, both learned the hard way on the same line:**
  **(a) The repair path is itself a multi-write and needs the same analysis.** A
  reconciler that clears a dangling claim races the operation that is still
  creating it, so it must be explicit, audited, and CAS-guarded exactly like the
  operations it repairs — otherwise the thing that fixes partial states becomes a
  source of them.
  **(b) A CAS guards the resource it names, not the operation you care about.**
  The obvious fix — an etag CAS on the head — does **not** close the reap-an-
  in-flight-grant race, because **the grant's second write touches the *binding*,
  not the head**, so nothing about it invalidates a head etag. What closes it is a
  **state transition on the shared resource that both parties must perform**:
  `CLAIMING → BOUND` as an explicit `finalize_head_claim`, with the reaper doing a
  state-CAS on the exact phase it observed. Then a concurrent finalize changes the
  phase and the reaper's clear fails, or the reaper wins and the grant's finalize
  fails and rolls back its own binding. **Exactly one wins.** When choosing a CAS,
  ask what the racing writer actually modifies — if it is a different object, the
  CAS is decoration.
- **A probe surviving a tree change is not the same as its *result* surviving.**
  Classifying a check as "position-independent" means it can be **re-run
  unchanged** against a moved tip — it does *not* mean the earlier answer still
  holds. Whether the result transfers depends on whether the behaviour changed,
  which is a separate question from whether the probe still applies. Where source
  is byte-identical the two coincide (subtree-hash equality is the strongest cheap
  proof: `services` and `packages` hashing equal across two commits covers every
  file, including ones nobody thought to name). Where source moved, a
  position-independent probe must still be **re-executed**. **The classification
  saves re-derivation, never re-execution** — treating it as saving both is how a
  stale result gets carried forward under the appearance of method.
- **A parser can fail *silently and confidently* — check its configuration before
  trusting its negatives.** A search for per-test Jest timeout overrides returned
  **zero**, twice, by two different methods. The regex missed the multi-line
  `},⏎ 15000,⏎)` form; the TypeScript AST rescan then **parsed `.tsx` files with
  `ScriptKind.TS` instead of `ScriptKind.TSX`**, so JSX like
  `<Component running={false} />` was read as binary expressions, scrambling the
  `it()` argument shape. **The AST did not error — it produced a wrong parse and
  answered from it.** Ground truth was five overrides, and the false negative
  inflated a ratio from 0.342 to 1.026, misidentifying which suite sat at the
  edge. Escalating from regex to AST felt like rigour and reproduced the same
  answer for a different reason, which is worse than one wrong method: **two
  independent tools agreeing is only evidence when they fail independently.**
- **Measure in the execution form that ships, or the number describes a scenario
  that never occurs.** The same test measured **2869 ms in isolation (ratio 0.57)
  and 5099 ms under full-suite contention (ratio 1.02, hard timeout)**. Isolation
  was never the failure mode — it was consistently 66/66 clean — so an isolated
  measurement of ~0.40 and a firing full-in-band gate are **both true at once**,
  and neither party is wrong. When two measurements of "the same thing" disagree,
  establish whether they share an execution form *before* trying to reconcile the
  values. Worth noting what settled it: a **passive** instrument (a jest-circus
  environment bracketing `test_fn_start → test_fn_success`) showed body time
  2869 ms vs reported 2872 ms — a 3 ms delta that killed the
  metric-definition hypothesis outright, converting *"we may be measuring
  different things"* into the harder and more useful *"we measure the same thing
  and disagree."*
- **Under structural typing, "separate types" is not a boundary — you must remove
  the method.** Python `Protocol`s match structurally, so two ports with *no
  inheritance relationship* still both accept any concrete class that happens to
  satisfy both. A store exposing `get` alongside its write surface therefore
  satisfied a read-only port and a control-plane port simultaneously, and a
  mis-wire type-checked cleanly. The fix is **composition, not renaming**: the
  control-plane adapter stops exposing `get` at all and *holds* a reader over the
  same storage (`self.reader.get`), so passing it where a reader is expected is a
  mypy error rather than a silent success. Verified in `main` —
  `InMemory`/`Cosmos…Store` expose `reader` + head/enumerate/write and **no
  `get`**; only the `…Reader` classes have it.
  **And the honest claim has three parts, none of which substitutes for another:**
  types give *"unreachable through this reference"*; the composition root gives
  *"never handed a write-capable object"*; RBAC gives *"denied even if reached."*
  Neither types nor composition give *"cannot be the wrong object"* — **only RBAC
  makes a wrong object harmless.**
- **Range-scoped review cannot see a defect in the baseline.** The `.py` +
  `requirements.txt` inclusion filter behind F1 survived a full day of adversarial
  review on **two** branches — including neutralization on one — because **it was
  never in a diff.** Every review was scoped to a commit range, and the filter
  predates the range on both lines. It was found by computing the shipped set
  against `.agentignore`, i.e. by asking about the **domain** rather than the
  **delta**. Pair every range review with at least one question that ignores the
  range.
- **Mutate the boundary of a guarantee, not just its current instance.** For an
  exclusion set, widen it; for a cap, raise it; for a filter, add a case. If the
  suite stays green, the test pins **today's value** rather than the rule.
- **An overstated finding costs more than a missed one.** The F1 figure was
  briefly inflated from 12 to 22 by counting files `.agentignore` excludes from
  packaging. An overstated blocker doesn't merely waste work — **it spends the
  credibility that makes the real findings actionable.** The fix was cheap and
  mechanical: replay the ignore file through git's own matcher instead of
  pattern-matching by eye.
- **A latent defect does not merely delay detection — it misattributes the
  investigation.** When a defect has no symptom, the eventual investigation lands
  on the commit that *removes the suppressant* and appears to break things, not
  on the one that introduced the defect months earlier. Latency is recoverable;
  misattribution burns the investigation. This is why a detector for the
  *condition* (e.g. the `check-attr` assertion guarding the golden `-text`
  exemption) is load-bearing rather than belt-and-braces: with no symptom to
  watch for, asserting the condition is the only available signal, and deleting
  it as "duplicative" leaves the property genuinely unguarded.
- **A completeness check is only as good as the file types it enumerates.** The
  silent-loss scan that recovered six lost definitions parsed every Python
  module's `ast` — and therefore could not see that the merge had deleted every
  release gate from `.github/workflows/ci.yml`. The most damaging loss of the
  whole integration was in the one file the checker was structurally incapable of
  reading. Enumerate the *universe* first, then choose the parser per type.
- **A green gate that nothing invokes is indistinguishable from no gate.** The
  suppression checker survived the merge intact as a script and was correct the
  whole time — it detected real drift the moment it was finally run. What was lost
  was the four lines of CI that called it. Check the *caller*, not just the
  mechanism.
- **Distinguish "the control cannot detect it" from "the control was not run."**
  Reading the stale committed contract suggested 92 files had silently escaped the
  partition. Running the checker showed the opposite: it recomputes from the tree
  and flags them correctly. The alarming conclusion was an artifact of trusting a
  committed snapshot over an execution.
- **Verify the tip with `git rev-parse` before reading any report.** Every workstream
  reported a stale SHA at least once; one branch was rebuilt ten times.
  `git branch --contains` is a *reachability* test, not a tip test — it passes for
  every commit in history.
- **Evidence must name its *range*, not only its SHA — correct evidence for a
  closed sub-range reads as current evidence once the range label is dropped.**
  A reviewer working `94de900..f36947b` held figures of 1261 tests / 478 stmts
  while the tip measured 1277 / 483, which looked like a stale report. It was not:
  the numbers were **correct for `94de900`**, which was the tip when they were
  declared and the base of the range the reviewer had been assigned. **The
  diagnosis drives the remedy and the two differ** — if evidence is stale the fix
  is *report fresher*; if it is correctly scoped to a superseded range the fix is
  *label the range*, and reporting fresher would not have prevented it. Getting
  this backwards installs a remedy that cannot work against the failure it targets.
- **Prefer a *produced* quantity over an *asserted* one when checking whether
  evidence matches its subject.** This is the general form of the test-count rule
  and the reason it works: **a SHA is asserted; a count is produced.** An asserted
  value can be copied from stale notes and survives the copy intact; a produced
  value is re-derived by the act of measuring and cannot be. The test count is
  merely the cheapest produced quantity available — coverage `Stmts` worked
  identically here (478 vs 483) and is a second instance of the same technique.
  **But treat a matching count as absence of evidence, not evidence of
  freshness.** A *changed* count proves a different tree; an *equal* count proves
  nothing. Measured on one chain: `94de900`→1261, `f86a855`→1264, `f2cc5ac`→1277,
  `f36947b`→1277 — so a report claiming `f2cc5ac` at 1277 would have looked
  self-consistent while naming the wrong commit, and the fingerprint would have
  *confirmed* the stale SHA. Worse than the "docs-only commit" framing suggests:
  `f2cc5ac`→`f36947b` changes a test file **materially** (+37/−10 in
  `test_agent_studio_capability_discovery_golden.py`) and still moves the count by
  zero, because it adds no new `test(`/`it(` declarations. **A tree can change
  substantively inside test files without the count noticing.** Pair it with
  `rev-parse` and never let it stand alone.
- **Neutralization over coverage.** 100% line-and-branch coexisted with a live
  blocker on lines proven executed.
- **When a property holds because something is absent, assert the absence** — the
  method set, the module set, the call count, the config default.
- **Ask what a fix stopped you from being able to see.** A remediation that
  removes a *symptom* disables every detector watching for that symptom. This is
  distinct from a hidden defect (an audit hunting for problems finds it) and from
  absent enforcement (found by asking "what is the domain?"). Here nothing is
  concealed and nothing is unenforced — the fix genuinely works — but the evidence
  channel for a *different, still-live* failure mode is destroyed as a side
  effect. Measured in both directions on this repo:

  ```
  exemption defeated, eol=lf present  -> mechanism test FIRES, outcome test BLIND
                                         (transport pin holds, 0 CRLF pairs)
  exemption intact, CRLF by another route -> outcome test FIRES, mechanism BLIND
                                         (check-attr still reports 'unset')
  ```

  So neither test is redundant: **each is the sole detector of a different
  failure mode**, and deleting either as duplicative removes the only thing that
  can see one of them.
- **Verify a claim even when you expect it to be true.** A docstring asserting a
  measured result is a claim like any other. The table in that test's docstring
  was re-derived independently and matched line for line — which is the step worth
  taking precisely because the statement looked correct.
- **When a control's stated rationale turns out to be false, restate the
  rationale — do not delete the control.** A revocation tombstone was documented
  as existing to preserve a succession counter. That reason became untrue once a
  separate HEAD record owned succession, and the next reader finding the argument
  false would reasonably have deleted the tombstone. It is still required for two
  *different* reasons, now stated in code: it preserves the revocation **audit
  record**, and it keeps "absent" **unambiguous** — without it a revoked client
  and a never-granted client are indistinguishable. **A control defended by a
  dead argument is more fragile than one with no comment at all**, because the
  dead argument invites its removal and supplies the justification.
- **Before writing a test for a finding, ask: is the property *enforced* today, or
  merely *true* today?** The answer decides the instrument, and getting it wrong
  produces a test that pins an accident.

  | | property | instrument |
  |---|---|---|
  | **Enforced but unguarded** | the mechanism exists and works; the risk is silent removal | **add the assertion** |
  | **Unenforced but currently true** | nothing implements it; it holds by circumstance | **change the structure** |

  Of the findings in §5, **N1** and **N2** are the first kind — the call and the
  sort are both present, and each survives mutation only because nothing asserts
  it. **F1**, the **replay provenance** gap and the **unescaped warning text** are
  the second: an assertion there would pass today, pass after someone adds the
  consumer that makes it dangerous, and pass right up until the failure. Escaping
  at the boundary makes the property impossible to violate, including for a sink
  nobody has written yet; a test only detects the sink that exists.

  **An owner handed five findings framed as "add a test" would write five tests,
  and three of them would encode the current accident as the invariant.**

  **The assertional column has the same trap one level down:** N2's test must use
  an **NFC-reordering path**, because `git ls-tree` emits raw-byte order and paths
  are NFC-normalised before sorting — so on ASCII the sort is a *no-op* and any
  ASCII fixture would be an assertion that cannot fail.

  **Ranked together, the five carried items compress to one sentence — and it is
  the sentence an implementer needs first:**

  ```
  N1      drift check installed      property enforced, unasserted
  N2      ordering cannot matter     property enforced, unasserted (inert on the tested class)
  F1      three sets must relate     property unenforced, currently true
  F-PROV  git ids resolved           property never claimed in code, claimed in prose
  replay  completed_replay = DENY    property true by default, nothing asserts the default
  ```

  **Four of the five are one test each. None is a code fix. The implementation is
  right; what is missing is the assertion that keeps it right.** Anyone opening §5
  should read that before the individual entries, because the entries describe
  defects and the summary describes the *shape* of the work — and the shape is what
  determines whether this is an afternoon or a rewrite.

  **The distinction that makes those rows writable as tests:** *a fact about the
  fixtures* tells you what the tests happen to contain; *a fact about the control*
  tells you what they **must** contain. N2 read as "breaks for non-ASCII" — a fact
  about the fixtures, and an observation. Restated as "the control is inert on the
  entire input class the tests use," it is a fact about the control, and a
  specification. **Only the second can be written as a test**, which is why every
  finding here was pushed toward the control-shaped phrasing before being recorded.
- **A stale verdict fails in the direction nobody audits.** A *wrong* verdict gets
  challenged on its content; a verdict that quietly stops describing the code
  keeps being cited, because the thing that changed is not in the document.
  **The cheapest guard: when the range moves, re-measure the *findings*, not the
  whole review.** On this program that cost three neutralization runs and saved a
  full re-review — and it confirmed N1 and N2 were still live at the new tip
  rather than inherited from the old one.
- **A principle stated abstractly is a hypothesis; it becomes a rule only once
  someone attempts it against a real tree.** And the companion clause is
  discovered by the **implementer**, not the author — not because the author was
  careless, but because the author holds the general case and the implementer
  holds the specific one, **and companion clauses live only in the specific.**
  Two rules in this document acquired theirs that way: the both-roots cross-check
  needed path normalization, and a `.gitattributes` recommendation was withdrawn
  outright once measured.
- **A striking result deserves more verification than a dull one.** The most-quoted
  number in this program was computed against the wrong denominator.
- **Grep the invariant, not the symbol name — a name search cannot observe the
  condition it was asked about.** The question was *"does `main` have the
  enumeration capability?"*; the grep tested *"does `main` contain this
  identifier?"* Those come apart the instant a rebuild renames, which is exactly
  what happened: `dataset_approvals_without_requester_principal` returned **0 hits**
  while `dataset_approvals_blocked_by_requester_attribution` — the same capability,
  renamed — sat in `workspace.py:1207` with four passing tests. **The document
  briefly recorded a false gap and instructed a hand-port**, which would have
  produced a *second* enumeration helper beside a working one: two APIs for one
  question, on the migration tool whose whole purpose is producing **one**
  trustworthy number. **The failure mode of a duplicated counter is two different
  counts, which is worse than no counter.**

  **This is the same defect as `= null` versus `NOT IS_DEFINED`, with the sign
  flipped** — a probe that cannot see the thing it is asked about. That one gave
  false *reassurance*; this one gave a false *alarm*. Apply the standard test —
  *what would have to be different for this check to fail?* — and a name grep fails
  **whenever the name changes, independent of whether the behaviour is present.**

  **The reliable form is to search for the behavioural invariant**, which cannot be
  renamed away: here `NOT IS_DEFINED` (`workspace.py:1227`, `1230`;
  `test_cosmos_workspace.py:1069`) locates the capability regardless of what the
  function is called. **Compare trees, not messages or parents — and compare
  capability, not symbol name.** A rebuild changes the tree *and* renames; git-level
  checks caught the first, and the symbol table was the one place a name was still
  trusted as a proxy.
- **Ask what a fix cost in visibility, not just what it fixed.** Remediation
  removes the conditions that made a problem observable, and nothing in a normal
  review surfaces that — the diff shows what was added, never what stopped being
  detectable. **"What did this fix stop us from being able to see?"** is not a
  question anyone asks by default, and on this program it was the only route to an
  entire finding class. The EOL remediation was correct and complete on its own
  terms; asking the question anyway is what found the cost. **Ask it of every
  successful fix, especially the clean ones** — a messy fix invites scrutiny, a
  clean one closes the file.
- **Probe your own formulation in the direction that would refute it, not the one
  that would confirm it.** Distinct from neutralizing code in both directions
  (above): this is about the *claim*. A reviewer here proposed "outcome for
  detection, mechanism for diagnosis," then ran both directions of the probe and
  **refuted it in about four minutes** — the same experiment that would have
  confirmed it killed it, because they ran the half that could say no. **Running
  only the confirming direction is how a wrong formulation becomes durable:** it
  accumulates supporting instances, none of which could ever have contradicted it,
  and the cost of the refuting half is usually one extra assertion. **A formulation
  that has only been confirmed has not been tested.**
- **A measurement tool that fails *silently* on a subset will under-report, and
  the subset is never random.** Three instances here, all costing a wrong number
  before being caught:
  - PowerShell `Get-Content $path` (without `-LiteralPath`) treats `[...]` as a
    wildcard, so `apps/web/src/app/api/backend/[...path]/route.test.ts` returns
    **null** rather than erroring. A repo-wide scan silently skips it. Bit twice.
  - An `ast`-based scan restricted to `.py` could not see release gates being
    deleted from `ci.yml`, so a silent CI regression passed the audit built to
    catch silent regressions.
  - `git diff --diff-filter=A main...<branch>` reports files added on **both**
    sides as branch-only, inflating "files `main` lacks" from **4** to **37**.

  **The common shape: the tool answers a question adjacent to the one asked, and
  returns a well-formed answer either way.** A null read looks like an empty file;
  a `.py` filter looks like a clean scan; a three-dot diff looks like a set
  difference. **None of them errors**, which is precisely why each survived to
  produce a number that was then quoted. Where a scan claims *repo-wide* coverage,
  verify the denominator independently — count the files the scan actually opened
  and compare it against `git ls-files`.
- **An invariant established early enough becomes a detector, and that beats
  foresight — which nobody has.** The provider adapter's no-trim fix was not
  anticipated. The author reached for `.strip()` as the obvious way to write a
  validity check, and *the moment they typed it* the question "am I deciding or
  transforming?" became unavoidable — because that module had already spent the day
  insisting provider-owned pins are returned **byte-for-byte**. The property was
  load-bearing in enough places that violating it felt wrong from inside.
  **This is a design-ordering argument, not a discipline one:** put the invariant in
  early and later changes must reckon with it, because the cost of breaking it is
  paid while typing rather than at review. A property asserted once in a test is
  checked after the fact; a property the surrounding code depends on everywhere is
  checked continuously, by whoever is writing the next line.
  **Corollary: the second instance of a fabrication is much easier to catch than
  the first** — the first establishes what the code refuses to do, and every later
  one is measured against it.
- **Where measurement is cheap, re-measure instead of arguing about the record —
  and say so plainly, because it is an economic call and not a principled one.**
  A disputed claim here was settled by a re-run costing ~15 machine-minutes, against
  an exchange about which message had said what. **The asymmetry decided it, not
  virtue.** That matters because the practice does *not* generalise: where
  measurement is expensive, "just measure it" becomes a way of deferring forever,
  and the honest move is the **labelled gap** — record what is unverified, name why,
  and let the reader weigh it. This document does that in three places, and each is
  more useful than a confident claim would have been. **Know which regime you are
  in before invoking the rule.**
- **Identical field names across two "independent" implementations are a
  fingerprint of a duplicated brief, not of copying — and the coordinator should
  suspect their own dispatch record first.** Two sessions produced
  `source_tree_digest`, `schema_version`, `inclusion_policy_version` and
  `manifest_digest`, plus matching NFC normalization, case-fold collision
  rejection and bare-CR handling. I judged that convergence implausible and asked
  whether one had read the other. **Neither had: both kickoff prompts specified
  those names verbatim.** The duplication was mine, and the tell I read as evidence
  of contact was evidence of a common source — *me*. **Before asking "did you copy
  this?", check what you handed out**, because the coordinator is the one party who
  can confirm it and the only one who will not think to look.
- **When a value is protected by one digest, a second witness over different bytes
  is the test that the first is load-bearing.** Proposed on the release-identity
  branch and worth keeping even though the branch was not adopted: store git's own
  `blob_id` (raw bytes) alongside `content_digest` (canonicalized bytes). They fail
  **independently** — a canonicalization bug moves one, a git-object read bug moves
  the other — so each cross-checks the other, where a single digest cross-checks
  nothing. **The acceptance test is the neutralization form**: corrupt one
  `blob_id` while every `content_digest` still validates, and require the load to
  fail closed. A test that cannot distinguish "second witness enforced" from
  "second witness stored and ignored" has not tested the second witness.
  Related and cheaper: **a recomputed walk absorbs set changes silently** — delete
  a covered file and the digest becomes a *different valid digest*. A committed
  exact set turns that into a CI failure naming the file.
- **An addition reads as a substitution, and it is not one.** Correct text appearing
  where you asked for a correction feels like the incorrect text was replaced —
  because the thing you were looking for is now there. It usually was *added
  alongside*. This single reflex produced **two independent failures here**: the
  `app.py` docstring that still says `action="consumed"` "must imply data really
  left" *next to* the correction, and a reviewer's own retracted claim, withdrawn
  when they realised they had inferred a replacement from the presence of the
  replacement text.
  **The general form: evidence of the desired state is not evidence of the
  undesired state's absence.** It applies to a reviewer reading a diff exactly as
  it applies to code. **So a correction ruling must name the deletion** — "delete
  the incorrect wording," never "document the correct behaviour" — and the test
  that enforces it must **assert the wrong text is gone**, not that the right text
  is present. A doc-assertion test that only checks for the correct string passes
  happily while the contradiction sits one line below it, which is the state
  `main` is in today.
- **"Was it approved?" and "was *this* approved?" are different questions, and only
  the second is answerable by measurement.** Ancestry proves a commit is reachable;
  it says nothing about whether the reviewed *content* survived. The check that
  answers it is one command — compare the **subtree hash** of the reviewed tag
  against the shipped tip:
  `git rev-parse <reviewed-tag>:services` vs `git rev-parse <shipped>:services`.
  Equal means the approval covers what shipped. Unequal means it does not, and the
  diff tells you by how much.
  **This is not hypothetical: it is how the dataset workstream's "APPROVED" was
  caught in this very document.** Three reviewed tags, all differing from the
  merged tree, **+168 lines of authorization code arriving after the last review** —
  found by the reviewer, on their own verdict, and reported rather than left to be
  cited. **A reviewer who volunteers that their approval does not cover what
  shipped is doing the most valuable thing a reviewer can do**, and the record must
  make that cheap to do rather than costly.
- **The failure this document describes is the failure this document committed.**
  §7 warned about verdicts outliving their range while §2 carried "APPROVED, 0
  blockers" for a tree no approval covered. The handoff is not exempt from its own
  practices, and it is the artifact least likely to be re-measured — every other
  claim here has code under it that someone will eventually run. **Prose is the
  only thing in a repository that cannot fail a test**, which makes confident prose
  the most durable place for an error to survive.

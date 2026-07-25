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
Twelve branches carry **zero** files `main` lacks, and on the shared files they
differ over, `main`'s versions are newer and larger (e.g.
`verify-playwright-runtime-coverage.mjs` 310 lines vs 244;
`run-e2e-coverage-gate.mjs` 242 vs 119) — and
`policy-gated-external-link.test.tsx` is byte-identical.

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
   **↳ Closed on that branch at `c90b9ce`, verified.** It now registers a
   `predeploy` hook (`scripts/predeploy.{ps1,sh}`) running
   `--verify-worktree --check`, so the hook set is
   `{predeploy, postdeploy, preprovision, postprovision}`. **This objection is no
   longer a reason to prefer the incumbent** and is recorded as closed so the
   verdict does not keep being cited after it stopped describing the code.
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
`"Package-eligible agent source differs from committed content"` and names
nothing, so an engineer who trips it at deploy time has to go find the divergence
by hand. Same detection, strictly worse diagnostics.

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
| tests | `test_enumeration_narrows_to_the_genuinely_affected_set`, `test_enumeration_helper_is_scoped_to_one_project_and_under_reports`, `test_legacy_documents_omit_the_key_so_null_comparison_finds_nothing` — all three present |

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

**On the repeated rebuilds of this work:** at least three commits exist on parent
`673985b` with the same message — `1affe85` (merged), `e0b5367` and `f574ab3`
(neither merged, and both *smaller* than `main`: −450 and −265 lines
respectively). **Compare trees, not messages or parents** — all three are
indistinguishable by both. Anything wanted from the unmerged pair must be checked
symbol-by-symbol against `main` first, because each is a *different build* of the
same work rather than a successor to it.

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

- **The `userEvent.setup()` conversion is now complete for the state lineage** —
  `c6370e5` landed after the first integration pass and is merged. **50 bare calls
  remain in `main`, and they belong to the agent-studio workstream, which never
  did this conversion at all:**

  ```
  21  agent-workspace.test.tsx
  15  agent-registry.test.tsx
  13  connections-view.test.tsx
   1  error.test.tsx
  ```

  `userEvent.setup()` defaults to `delay: 0`, which awaits a real
  `setTimeout(…, 0)` between **every** dispatched event; a test with ~25
  interactions accumulates 100+ hops that cost real time under a loaded event
  loop. Converting to `delay: null` preserves every event and its ordering.
  **Verify per file before converting** — no `setTimeout`/`setInterval`/
  `requestAnimationFrame` and no debounce in the component under test — rather
  than assuming the state lineage's result generalises.
- **The fix was applied to the suite that was not at risk.** `studio-components`
  carries a `15000` override on its heaviest test (ratio 0.34). `research-workbench`
  has **no override**, sits at **0.735**, retains all 22 unconverted sites, and is
  the historical failing site. `research-markdown.integration` is at **0.523** and is
  in neither the fix nor the report.
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

- Growth requires `--suppression-addition-reason`, written into the artifact.
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

---

## 6. Environment

- A remote named `Main` collides case-insensitively with branch `main` on Windows,
  so `git rev-parse main` warns about ambiguity and `git worktree add` from `main`
  fails. Fix: `git remote rename Main origin`.
- `uv sync --all-packages --all-extras` is required; a plain `uv sync` leaves
  `fastapi` and the workspace packages uninstalled.

## 7. Practices worth keeping

From the review program that produced §5.

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
- **Report the test count next to the SHA.** A SHA is asserted; a count is produced
  by the run and cannot be copied from stale notes.
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

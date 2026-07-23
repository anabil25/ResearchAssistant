# Research Assistant

An evidence-governed Research Assistant accelerator for higher education,
standardized on Microsoft Foundry, Microsoft Agent Framework, Azure AI Search,
Document Intelligence, Durable Task Scheduler, and Azure Container Apps.

The solution combines the strongest product ideas from Feynman and Vandalizer
without mixing GPL code, and applies the repeatable `azd`, managed identity,
observability, and validation lessons from the ITHelpdesk accelerator.

## Capabilities

- **Literature Studio** — protocol, multi-source search, screening and
  deduplication, extraction matrix, synthesis, and claim audit.
- **Grant Studio** — opportunity intake, requirement matrix, verified project
  facts, specific aims, section drafting, compliance, red team, and approval.
- **Matching Explorer** — hard filters, entity resolution, deterministic
  evidence-weighted scores, comparison, and confirmed shortlist.
- **Dataset Lab** — asset selection, schema/quality profile, analysis plan,
  deterministic metrics, model interpretation, and approved scale-out.
- **Institutional Q&A** — identity-filtered retrieval, effective-version and
  conflict resolution, citation-backed answer or explicit abstention.
- **Workflow Automation** — templates, typed DAG steps, triggers, bounded
  retries, approval gates, dry runs, and durable history.

The workbench also includes a governed **Library**, authoritative **Runs &
Approvals**, visible connector setup, and complete project **Settings** for
agents/models, retrieval, governance, and evaluation.

## Online research and industry connectors

Online research is **off by default** and can be enabled only on a public
literature, grant-opportunity, or matching run. Offline specialists are
separate tool-free deployments; three public-online deployments receive
Foundry Web Search. Institutional and dataset workflows cannot enable public
web tools. Web results remain untrusted until stored and verified and can flow
outside the Azure compliance/geographic boundary under Grounding with Bing
terms.

The API applies saved connector enablement and specialist assignments before
retrieving bounded public metadata:

| Domain | Sources |
|--------|---------|
| Scholarly literature | PubMed/NCBI, Europe PMC, Crossref, OpenAlex, arXiv, Semantic Scholar |
| Trials and funding | ClinicalTrials.gov v2, Grants.gov, NIH RePORTER |
| Datasets and identifiers | DataCite, ORCID, ROR |

Connectors return bounded metadata and canonical URLs, not bulk copyrighted
full text. `NCBI_API_KEY` and `SEMANTIC_SCHOLAR_API_KEY` are optional; supply
them only through an approved secret connection. Run the live transport check
with `uv run python scripts/smoke_connectors.py`.

## Architecture

Nine separately deployable Microsoft Agent Framework Hosted Agents form the
reasoning layer behind six studios:

| Agent | Responsibility | Model tier |
|-------|----------------|------------|
| `research-coordinator` | Routes bounded tasks and preserves specialist evidence | `gpt-5.4-mini` |
| `literature-agent` | Literature comparison and skeptical synthesis | `gpt-5.6-sol` |
| `grant-agent` | Requirements and evidence-bounded grant drafting | `gpt-5.6-sol` |
| `matching-agent` | PI, facility, equipment, method, and template matching | `gpt-5.4-mini` |
| `dataset-agent` | Explanation of deterministic data/notebook profiles | `gpt-5.4-mini` |
| `institution-agent` | Authorized institutional guidance | `gpt-5.4-mini` |
| `literature-online-agent` | Public scholarly metadata and web research | `gpt-5.4-mini` |
| `grant-online-agent` | Public opportunity metadata and web verification | `gpt-5.4-mini` |
| `matching-online-agent` | Public researcher/organization metadata leads | `gpt-5.4-mini` |

All agents use direct-code deployment, the Foundry Responses protocol `2.0.0`,
dedicated endpoints, and dedicated Entra agent identities. The implementation
does not use the retiring shared-endpoint `agent_reference` pattern.

The API—not the model—derives tenant identity, filters Azure AI Search, resolves
citations, calculates match scores, and records approvals. Hosted Agent text is
supplemental `model_analysis` until its source IDs resolve. Agents do not query
multi-tenant Search directly.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the complete topology and data flows.

## Prerequisites

| Tool | Supported baseline |
|------|--------------------|
| Azure subscription | Contributor plus role-assignment permission |
| Azure CLI | 2.84+ |
| Azure Developer CLI | 1.27+ |
| `azure.ai.agents` azd extension | 1.0.0-beta.5+ |
| Python | 3.12 for API/worker; Hosted Agent build uses Python 3.13 |
| uv | 0.10+ |
| Node.js | 24 LTS |
| Docker | Needed for web/API/worker deployment; not needed for direct-code agents |

The deployment plan and checked subscription/region capacity are recorded in
[`.azure/deployment-plan.md`](.azure/deployment-plan.md).

## Local development

Install dependencies:

```powershell
uv sync --all-packages --group dev
Set-Location apps\web
npm ci
```

Start the API:

```powershell
uv run --package research-assistant-api uvicorn research_assistant_api.app:app --reload --port 8100
```

Start the web app in a second terminal:

```powershell
Set-Location apps\web
$env:INTERNAL_API_URL = "http://127.0.0.1:8100"
npm run dev
```

Open <http://localhost:3000>. Local execution uses a deterministic synthetic
corpus, in-memory workspace/blob adapters, and makes no model calls. Azure uses
Cosmos DB for operational state, Durable Task Scheduler for every new run, and
Blob Storage through a private endpoint for uploaded source files.

## Runtime ingestion

Library uploads accept bounded PDF, text, Markdown, CSV, or JSON files up to
20 MB. The API records the checksum, content type, access scope, license, and
durable instance before scheduling:

`upload -> Blob -> layout extraction -> structural chunks -> embeddings -> Search`

PDFs use Document Intelligence `prebuilt-layout` Markdown output. Text formats
use deterministic paragraph-aware chunking. The worker writes embeddings to
Azure AI Search and marks the Cosmos Library/run records ready only after every
chunk is accepted.

## Quality gates

```powershell
uv run ruff check packages services agents scripts tests
uv run mypy packages/research_core/src packages/research_connectors/src services/api/src services/worker/src agents/shared scripts tests
uv run pytest -q
uv run pip-audit
az bicep build --file infra\main.bicep --stdout

Set-Location apps\web
npm run ci
npm run test:e2e
npm audit --audit-level=moderate
```

The Playwright configuration starts both the API and the Next.js app and tests
all six studios, Library ingestion, Runs/Approvals, connector settings, and
mobile navigation through the BFF.

## Azure deployment

The project uses one `azure.yaml` lifecycle for infrastructure, nine Hosted
Agents, and three Container Apps.

```powershell
azd auth login
az login
azd up
```

On the first run, `azd up` prompts for a unique environment name, subscription,
and Azure location. The selected location drives Foundry, models, Search,
Container Apps, Document Intelligence, Cosmos DB, Storage, Durable Task, and
monitoring; no deployment region is pinned in the repository.

`azd up` performs:

1. Provider, service/SKU, and exact model availability preflight for the
   selected region.
2. Bicep provisioning with managed identities and least-privilege RBAC.
3. Search index creation, real Foundry embeddings, and synthetic corpus upload
   through an automatically created, isolated `.venv-provision`.
4. Direct-code Hosted Agent deployment and immutable version creation.
5. Container build/deployment for web, API, and worker.
6. VNet-integrated Container Apps plus private Blob/Cosmos endpoints and DNS.

Storage and Cosmos public access are disabled by the accelerator itself, so the
runtime path is deterministic even when a subscription has no enforcing policy.
Search, Document Intelligence, and Foundry remain public endpoints protected by
managed identity in this POC profile.

After deployment, run one smoke invocation per Hosted Agent and generate/run
the evaluation suites from `agents/evals/`. Deployment execution must follow
the `azure-validate` then `azure-deploy` skill workflow.

## Demo sequence

1. Open **Literature Studio**, lock the protocol, and inspect screening,
   extraction, synthesis, and resolved evidence.
2. Open **Grant Studio** to show the requirement matrix, fact gaps, readiness,
   red-team boundary, and blocked export.
3. Open **Matching Explorer** and inspect each deterministic score component.
4. Open **Dataset Lab** to show computed schema/quality and the scale-out
   estimate/approval boundary.
5. Ask **Institutional Q&A** a supported and unsupported question; note that
   public web is unavailable.
6. Open **Workflow Automation** to validate/dry-run the typed DAG.
7. Ingest a Library file, inspect its durable run, then review an exact gated
   action under **Runs & Approvals**.
8. Open **Project Settings → Connectors** to inspect all 12 source assignments,
   terms, secret state, and bounded connection tests.

## Data and safety

The included corpus is synthetic and CC0. Do not replace it with institutional
or regulated data until access control, retention, network isolation, data
residency, authentication, and compliance requirements are approved.

See [SECURITY.md](SECURITY.md) for the POC boundary and production hardening
requirements.

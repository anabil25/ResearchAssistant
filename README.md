# Research Assistant — Azure Solution Accelerator

A one-click (`azd up`) evidence-governed Research Assistant for higher
education, standardized on Microsoft Foundry, Microsoft Agent Framework,
Azure AI Search, Document Intelligence, and Azure Container Apps.

The solution combines the strongest product ideas from the academic research
tooling ecosystem without mixing GPL code, and applies the repeatable `azd`,
managed identity, observability, and validation patterns from
[ITHelpdesk](https://github.com/abKrazy/ITHelpdesk). Researchers chat with a
Next.js workbench that calls a FastAPI backend, which coordinates nine
Foundry Hosted Agents across six research studios.

> 📐 [ARCHITECTURE.md](ARCHITECTURE.md) is the source of truth for
> components, data flows, and cross-component contracts. This README is the
> deploy & run guide for a fresh clone.
>
> 🌐 Prefer a browsable version? Open the [project landing page](https://anabil25.github.io/ResearchAssistant/)
> — this README and the architecture docs with a left-nav menu and interactive
> diagram.

## Quick start

Choose one path:

- **Deploy everything to Azure (one click):** [Deploy (the one-click path)](#deploy-the-one-click-path)
- **Run locally for development:** [Local development](#local-development)

### 60-second preflight

Run these before either path:

```powershell
azd version
az version
python --version
uv --version
node --version
```

If `uv` is missing, install it first:

- Docs: https://docs.astral.sh/uv/getting-started/installation/
- Windows (winget): `winget install --id AstralSh.uv -e`

### Quick path A: one-click Azure deploy

```powershell
git clone https://github.com/anabil25/ResearchAssistant.git
Set-Location ResearchAssistant
azd auth login
az login
azd up
```

### Quick path B: local development run

```powershell
git clone https://github.com/anabil25/ResearchAssistant.git
Set-Location ResearchAssistant
uv sync --all-packages --group dev
Set-Location apps\web
npm ci
```

Start the API (terminal 1):

```powershell
Set-Location <repo-root>
uv run --package research-assistant-api uvicorn research_assistant_api.app:app --reload --port 8100
```

Start the web app (terminal 2):

```powershell
Set-Location apps\web
$env:INTERNAL_API_URL = "http://127.0.0.1:8100"
npm run dev
```

Open <http://localhost:3000>.

## What it is

### The 6 studios (and what they do)

| Studio | What the researcher can do |
|--------|---------------------------|
| **Literature Studio** | Lock the protocol, run multi-source search, screen and deduplicate, build the extraction matrix, synthesize, and audit every claim. |
| **Grant Studio** | Ingest an opportunity, build the requirement matrix, surface fact gaps in project data, draft specific aims and sections, run compliance and red-team checks, and gate export on approval. |
| **Matching Explorer** | Apply hard filters, resolve entities, compute deterministic evidence-weighted PI / facility / method scores, compare candidates, and confirm the shortlist. |
| **Dataset Lab** | Select assets, profile schema and quality, generate an analysis plan, compute deterministic metrics, explain model output, and gate scale-out on approval. |
| **Institutional Q&A** | Get identity-filtered, citation-backed answers from policy and handbook documents; the agent abstains explicitly when a question is outside its authorized scope. |
| **Workflow Automation** | Build templates, define typed DAG steps, set triggers, configure bounded retries and approval gates, dry-run, and maintain durable history. |

The workbench also includes a governed **Library**, authoritative **Runs &
Approvals**, visible connector setup, and complete project **Settings** for
agents/models, retrieval, governance, and evaluation.

### Online research and industry connectors

Online research is **off by default** and can be enabled only on a public
literature, grant-opportunity, or matching run. Offline specialists are
separate tool-free deployments; three public-online deployments receive
Foundry Web Search. Institutional and dataset workflows cannot enable public
web tools. Web results remain untrusted until stored and verified, and can
flow outside the Azure compliance/geographic boundary under Grounding with
Bing terms.

The API applies saved connector enablement and specialist assignments before
retrieving bounded public metadata:

| Domain | Sources |
|--------|---------|
| Scholarly literature | PubMed/NCBI, Europe PMC, Crossref, OpenAlex, arXiv, Semantic Scholar |
| Trials and funding | ClinicalTrials.gov v2, Grants.gov, NIH RePORTER |
| Datasets and identifiers | DataCite, ORCID, ROR |

Connectors return bounded metadata and canonical URLs, not bulk copyrighted
full text. A deployed administrator can add the optional Semantic Scholar key
in **Settings > Connections**; the API writes it directly to APIM and never
stores or returns it. Run the local live transport check with
`uv run python scripts/smoke_connectors.py`.

## Architecture

Nine separately deployable Microsoft Agent Framework Hosted Agents form the
reasoning layer behind the six studios:

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

Agent manifests in source control are authority-free templates. `azure.yaml`
injects the real workspace tenant and Foundry project name into every Hosted
Agent as `RESEARCH_WORKSPACE_TENANT_ID` and `RESEARCH_WORKSPACE_PROJECT_ID`;
startup uses them to produce the scoped manifest and immutable release
identity. Capability-bearing agents fail closed when either value is absent.

The API — not the model — derives tenant identity, filters Azure AI Search,
resolves citations, calculates match scores, and records approvals. Hosted
Agent text is supplemental `model_analysis` until its source IDs resolve.
Agents do not query multi-tenant Search directly.

Dataset compute additionally requires an application-installed durable
`ApprovalContextResolverFactory`. The Azure Container App sets
`RESEARCH_REQUIRE_APPROVAL_CONTEXT_RESOLVER=true`, so its backend composition
must install that adapter before startup; absence intentionally prevents the
API from serving rather than accepting client-supplied approval authority.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the complete topology and data
flows.

## Prerequisites (read this in full — it prevents 90% of deploy failures)

### 1. Tooling + minimum versions

| Tool | Minimum version | Check | Install |
|------|----------------|-------|---------|
| Azure Developer CLI (`azd`) | 1.27+ | `azd version` | https://aka.ms/azd-install |
| Azure CLI (`az`) | 2.84+ | `az version` | https://aka.ms/azure-cli |
| `azure.ai.agents` azd extension | 1.0.0-beta.7+ | `azd extension list` | `azd extension add azure.ai.agents` |
| Python | 3.12 (API/ingestion); 3.13 (agent build) | `python --version` | https://www.python.org/downloads/ |
| `uv` | 0.10+ | `uv --version` | https://docs.astral.sh/uv/getting-started/installation/ |
| Node.js | 24 LTS | `node --version` | https://nodejs.org/ |
| Docker | latest stable | `docker --version` | Needed for web/API container build; not needed for direct-code agents |
| git | 2.30+ | `git --version` | https://git-scm.com/downloads |

### 2. Azure subscription + RBAC roles the deploying user needs

`infra/main.bicep` is subscription-scoped: `azd up` creates the resource
group, all resources, and role assignments wiring the managed identity to
Search, Storage, Blob, Foundry, Cosmos, and Key Vault.

Creating role assignments requires `Microsoft.Authorization/roleAssignments/write`.
The deploying identity needs one of:

- **Owner** on the target subscription (simplest — includes RG creation and
  role assignment), **or**
- **Contributor** plus **User Access Administrator** (or Role Based Access
  Control Administrator) on the subscription.

> ⚠️ Plain **Contributor is NOT enough** — it will fail on role assignments
> in the Bicep modules with an authorization error.

Every resource `azd up` creates (single resource group `rg-<env>`) and the
roles it assigns:

| Resource | ARM type | Role(s) assigned to managed identity |
|----------|----------|--------------------------------------|
| Resource Group | `Microsoft.Resources/resourceGroups` | — |
| Managed Identity | `Microsoft.ManagedIdentity/userAssignedIdentities` | All other roles are granted to this principal |
| Log Analytics + App Insights | `Microsoft.OperationalInsights/workspaces`, `Microsoft.Insights/components` | — |
| Key Vault | `Microsoft.KeyVault/vaults` | Key Vault Secrets User |
| Storage + private endpoint | `Microsoft.Storage/storageAccounts` (Standard, Hot) | Storage Blob Data Contributor |
| Azure AI Search | `Microsoft.Search/searchServices` | Search Index Data Contributor + Search Service Contributor |
| Azure AI Foundry (+ project + model deployments) | `Microsoft.CognitiveServices/accounts` (kind AIServices) | Azure AI Developer + Cognitive Services OpenAI User |
| Document Intelligence | `Microsoft.CognitiveServices/accounts` (kind FormRecognizer) | Cognitive Services User |
| Cosmos DB + private endpoint | `Microsoft.DocumentDB/databaseAccounts` | Cosmos DB Built-in Data Contributor |
| Container Apps Environment + VNet | `Microsoft.App/managedEnvironments` | — |
| Container Apps (web + api) | `Microsoft.App/containerApps` | — |

### 3. Azure AI Foundry model deployment quota

`infra/main.bicep` deploys the following models in your chosen region:

| Model | Deployment type | Capacity | Config param |
|-------|----------------|----------|-------------|
| `gpt-5.4-mini` (coordinator, matching, dataset, institution, online agents) | GlobalStandard | 30 (30 K TPM) | `chatMiniModelDeploymentName` |
| `gpt-5.6-sol` (literature, grant agents) | GlobalStandard | 30 (30 K TPM) | `chatSolModelDeploymentName` |
| `text-embedding-3-large` (Search embeddings) | Standard | 30 (30 K TPM) | `embeddingModelDeploymentName` |

Model version is not pinned; Foundry uses the current default
(`versionUpgradeOption: OnceNewDefaultVersionAvailable`).

You must have quota for all models, at these capacities, in your chosen
region. To check / request:

```bash
# List available models + your quota in a region
az cognitiveservices account list-models \
  --name <foundry-account> --resource-group <rg> 2>/dev/null

# Or use the Foundry / AI portal quota blade:
#   https://ai.azure.com  ->  Management center  ->  Quota
# Request more: Azure portal -> Quotas -> Cognitive Services / Azure OpenAI
```

If quota is short, lower `capacity` (e.g. to 10) or pick a region with headroom.

### 4. Resource provider registrations

Ensure these providers are **Registered** on the subscription (portal →
Subscription → Resource providers, or the commands below):

```bash
az provider register --namespace Microsoft.CognitiveServices
az provider register --namespace Microsoft.Search
az provider register --namespace Microsoft.App
# Also used: Microsoft.Web, Microsoft.KeyVault, Microsoft.Storage,
#            Microsoft.DocumentDB, Microsoft.OperationalInsights,
#            Microsoft.Insights, Microsoft.ManagedIdentity,
#            Microsoft.Authorization, Microsoft.Network
```

### 5. Region availability caveats

> ⚠️ The single most important choice. Foundry Hosted Agents (Preview) are
> only available in a narrow subset of regions — narrower than Prompt Agents
> or the base models.

Pick a region that supports all of:

- **Foundry Hosted Agents (Preview)** — currently East US 2, North Central
  US, Sweden Central, West US, West US 3. This is the binding constraint.
  See [Hosted Agents region availability](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents#region-availability).
- **`gpt-5.4-mini` / `gpt-5.6-sol` (GlobalStandard) and
  `text-embedding-3-large` (Standard)** in Azure AI Foundry — see
  [model region availability](https://learn.microsoft.com/azure/ai-services/openai/concepts/models).
  Some Hosted-Agents regions (e.g. North Central US, West US) do not offer
  `text-embedding-3-large` on the Standard SKU — verify before choosing.
- **Azure AI Search** — capacity in Hosted-Agents regions is sometimes tight.
  If `azd up` fails with `ResourcesForSkuUnavailable`, re-run `azd up`
  (idempotent) or pick another region.
- **Azure Container Apps** + **Document Intelligence** — available in all of
  the above.

Well-tested choices: **Sweden Central**, **West US 3**, **East US 2**.

Check model quota and Search capacity first:

```bash
az cognitiveservices usage list --location swedencentral \
  --query "[?contains(name.value,'gpt-5.4') || contains(name.value,'gpt-5.6') || \
contains(name.value,'text-embedding-3-large')].{name:name.value,current:currentValue,limit:limit}" \
  -o table
```

**Governed / policy-locked subscriptions.** Some enterprise subscriptions
enforce Azure Policies that disable public network access on PaaS data
services. Storage and Cosmos use private endpoints by default in this
accelerator, so those are unaffected. If Search or Foundry are locked behind
network policy, `postprovision` (which runs from your machine) will fail with
a connection timeout. See [Deploying into a governed / network-restricted
subscription](#deploying-into-a-governed--network-restricted-subscription).

### 6. Optional API keys

No API key is required for `azd up`.

For the deployed app, a research administrator can add or clear the optional
Semantic Scholar key in **Settings > Connections** after deployment. The API
stores it as an APIM secret named value. Bicep and repeat deployments never
own or overwrite its value.

For the local direct-connector smoke check only, `NCBI_API_KEY` and
`SEMANTIC_SCHOLAR_API_KEY` can be set in the current shell before running
`uv run python scripts/smoke_connectors.py`. Never commit either key.

### 7. Cost note (not free)

A demo deployment is inexpensive but not free. Rough drivers:

- **Azure AI Foundry model usage**: pay-per-token for `gpt-5.4-mini`,
  `gpt-5.6-sol`, and embeddings — cents for a demo, scales with research
  traffic.
- **Azure Container Apps**: scales to zero when idle; a low-traffic instance
  costs a few dollars/month.
- **Azure AI Search**: Basic SKU, billed while provisioned (~$0.10/hour).
- **Document Intelligence**: pay-per-page for ingestion.
- **Cosmos DB + Storage + Key Vault + Log Analytics / App Insights**: a few
  dollars/day combined.

Run `azd down` when done to stop all charges (see [Clean up](#clean-up)).

---

## Deploy (the one-click path)

```bash
# 1. Clone
git clone https://github.com/anabil25/ResearchAssistant.git
cd ResearchAssistant

# 2. Sign in (both CLIs)
azd auth login
az login   # ensures az-based operations (bicep, quota checks) have context

# 3. Deploy everything
azd up
```

**Prompts you'll see during `azd up`:**

1. **Environment name** — a short name; drives `rg-<name>` and the resource
   token.
2. **Azure subscription** — pick the one where you have Owner / UAA.
3. **Region** — pick one satisfying the model + Hosted Agent availability
   above.

**What happens next (unattended):**

- `azd provision` runs `infra/main.bicep` — creates the RG, all resources,
  model deployments, role assignments, VNet, private Blob/Cosmos endpoints,
  and DNS.
- `postprovision` hook then:
  1. Creates an empty evidence Search index; ingestion remains an explicit
     user action.
  2. Reconciles mutable APIM connector APIs, policies, MCP surfaces, and
     missing optional secret slots without overwriting user-managed keys.
  3. Reconciles Foundry connections, Toolboxes, memory, and deployment data.
- `azd deploy --all` deploys all nine direct-code Hosted Agents and remotely
  builds the FastAPI and Next.js Container App images.
- `postdeploy` publishes Hosted Agent IDs and grants each runtime identity its
  required Foundry role. Provisioning fails closed before deployment if any
  required Search, APIM, Toolbox, memory, or ACR-readiness step does not
  complete, so agents never deploy with missing generated inputs.

Open the workbench — when `azd up` finishes it prints the Container App URL.
You can also fetch it any time:

```powershell
azd env get-value SERVICE_WEB_URI
```

Open that URL in a browser and start researching.

## Validate your deployment

Run each sample prompt in the workbench and confirm the expected result:

| Studio | Sample prompt | Expected result |
|--------|--------------|-----------------|
| Literature Studio | "Search for RCTs on metformin and cardiovascular outcomes since 2020" | Protocol locked; search results returned with PubMed / Europe PMC citations; screening matrix populated. |
| Grant Studio | "Identify requirements for NIH R01 PA-24-001" | Requirement matrix built; fact gaps against project facts surfaced; specific aims draft offered. |
| Matching Explorer | "Find PIs at our institution matching computational genomics" | Hard-filter pass; deterministic match scores computed; comparison table shown. |
| Dataset Lab | "Profile the NHANES 2017–2020 dataset" | Schema / quality profile computed; analysis plan offered; scale-out gated on approval. |
| Institutional Q&A | "What is our IRB expedited review threshold?" | Citation-backed answer from policy index, or explicit abstention if unsupported. |
| Institutional Q&A | "Browse the web for clinical trial news" | Refused — web tools are disabled for institutional workflows. |

## Local development

> Important: Local development in this repository requires `uv`.
> If `uv` is not installed, dependency installation will fail.
> Check first with `uv --version`.

Install dependencies:

```powershell
uv sync --all-packages --group dev
Set-Location apps\web
npm ci
```

If `uv` is missing, install it first:

- Docs: https://docs.astral.sh/uv/getting-started/installation/
- Windows (winget): `winget install --id AstralSh.uv -e`

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
corpus, an anonymous identity-scoped workspace/blob sandbox, and makes no
model calls. Azure uses Cosmos DB for operational state and Blob Storage
through a private endpoint for uploaded source files. Library ingestion runs
as a process-local API background task, without durable retry or restart
recovery. Personal projects and the active-project preference are stored in
the already-deployed tenant-partitioned Cosmos `research/projects` container;
the feature does not add a SQLite, SQL, or other persistence resource.

## Runtime ingestion

Library uploads accept bounded PDF, text, Markdown, CSV, or JSON files up to
20 MB. The API records the checksum, content type, access scope, license, and
run identifier before submitting a process-local background task:

```
upload -> Blob -> layout extraction -> structural chunks -> embeddings -> Search
```

PDFs use Document Intelligence `prebuilt-layout` Markdown output. Text
formats use deterministic paragraph-aware chunking. The API ingestion path
writes embeddings to Azure AI Search and marks the Cosmos Library/run records
ready only after every chunk is accepted. If the API process stops during
ingestion, the task is not resumed automatically.

## Quality gates

```powershell
uv run ruff check packages services agents scripts tests
uv run mypy packages/research_core/src packages/research_connectors/src services/api/src services/worker/src agents/shared scripts tests
uv run pytest -q --cov --cov-report=term-missing --cov-report=html:coverage/python/html --cov-report=json:coverage/python/coverage.json --cov-report=xml:coverage/python/coverage.xml --cov-fail-under=100
uv run pip-audit
az bicep build --file infra\main.bicep --stdout

Set-Location apps\web
npm run ci
npm run test:e2e
npm audit --audit-level=moderate
```

The Python and Jest coverage commands enforce 100% line and branch coverage;
Jest also enforces 100% statements and functions. Python writes retained HTML,
JSON, and XML reports; Jest writes HTML/lcov, JSON, and Cobertura reports.
The Playwright configuration starts both backend services and the Next.js app,
retains behavioral and WCAG assertions, fails on unexpected browser console or
request errors, and machine-validates every manifest Playwright ID against an
executable test title. It writes 42 desktop, tablet, and mobile screenshots
for core, loading, empty, error, and authorization states under
`apps/web/test-results`.

> **Note:** `uv run pytest --cov-fail-under=100` on its own enforces nothing:
> `[tool.pytest.ini_options] addopts` does not include `--cov`, so `pytest-cov`
> never activates and the threshold has no coverage data to check. It exits 0
> and prints no coverage table. Use the full command above.

### Running the E2E gate concurrently

`npm run test:e2e:gate` is safe to run concurrently, both across separate
checkouts/worktrees on one machine and twice within a single checkout. Each
invocation gets its own:

- ephemeral ports for the gateway, API, and web server, allocated as one
  simultaneously-bound set and held under a cross-process file lock
  (`src/testing/port-lock.ts`);
- Playwright `outputDir` and JSON report under `test-results/gate-<uuid>/`;
- an HTML report directory under `playwright-report/gate-<uuid>/` — a
  *sibling* of the output directory, never a child (Playwright refuses to
  start if the HTML reporter's folder is inside the test output folder);
- Next.js build directory under `.next-gate/gate-<uuid>/` — required because
  two concurrent `next build` calls into the same `.next` fail with
  `Another next build process is already running`.

Plain `npm run test:e2e` (without the gate wrapper) still uses the shared
`.next`, `test-results/`, and `playwright-report/` paths and is therefore
**not** safe to run concurrently with itself or with a gate invocation in the
same checkout.

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
8. Open **Project Settings → Connectors** to inspect all 12 source
   assignments, terms, secret state, and bounded connection tests.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `azd up` fails on a role assignment (`AuthorizationFailed`, `RoleAssignmentUpdateNotPermitted`) | Deploying user lacks role-assignment rights | Grant Owner, or Contributor + User Access Administrator, on the subscription (see Prerequisites §2). |
| Model deployment fails with a quota / capacity error | No quota for `gpt-5.4-mini`, `gpt-5.6-sol`, or `text-embedding-3-large` in the region | Request quota, lower `capacity` in `main.bicep`, or choose another region (Prerequisites §3). |
| `MissingSubscriptionRegistration` / provider not registered | First-time subscription | `az provider register` the namespaces in Prerequisites §4, then retry. |
| Model / Search "not available in region" | Region doesn't offer that SKU/model | Redeploy to a supported region (Prerequisites §5), e.g. East US 2 or Sweden Central. |
| `postprovision` fails with `search.windows.net timed out` or endpoint unreachable | Governed subscription's Azure Policy disabled public network access; `postprovision` runs from your machine | See [Deploying into a governed / network-restricted subscription](#deploying-into-a-governed--network-restricted-subscription). Fastest fix: run `azd up` from Azure Cloud Shell. |
| `postprovision` warns `KB blob upload skipped (AuthorizationFailure)` | Storage Blob Data Contributor role assignment hadn't propagated when `postprovision` ran | Non-fatal — `postprovision` retries with backoff. Re-run `azd provision` once the role has propagated. |
| Hosted Agent setup fails with `cannot import name '…'` | Drifted package in the deployer's Python environment | `postprovision` builds an isolated `.venv-provision/` with exact pins — ensure Python 3.12+ is on `PATH` and re-run `azd provision`. |
| Workbench loads but chat errors | `postprovision` didn't finish (no index / agents), or the web app cannot reach the API | Re-run `azd provision` (idempotent), verify `INTERNAL_API_URL` app setting, and check `azd env get-value SERVICE_WEB_URI`. |
| Semantic Scholar returns limited/empty results | Anonymous provider quota is exhausted | Add the optional key in **Settings > Connections**, then run the connector test. |
| `pip-audit` flags a vulnerability | Dependency CVE in the lockfile | Run `uv lock --upgrade-package <pkg>` and re-run the quality gate. |

### Deploying into a governed / network-restricted subscription

The `postprovision` hook does the data-plane wiring that Bicep can't: it
creates the empty AI Search index and reconciles connector, Toolbox, memory,
and Foundry project dependencies. That work runs from the machine that ran
`azd up`, over the public internet, using your `az login` identity. It
therefore needs network line-of-sight to the AI Search and Foundry endpoints.

Many enterprise subscriptions apply an Azure Policy that disables public
network access on PaaS data services. When that happens, a laptop simply
cannot reach the Search or Foundry endpoint — you'll see a connect timeout,
even though `azd provision` itself succeeded.

Three options, easiest first:

1. **Run `azd up` from [Azure Cloud Shell](https://shell.azure.com)** — code
   running inside Azure is treated as a trusted Azure service, so it reaches
   the endpoints even when public access is off. This is the recommended path
   for locked-down tenants and needs no infra changes. (Clone the repo,
   `azd auth login`, `azd up`.)
2. **Allow your client on the resources** — enable public network access on
   the AI Search service (or add your egress IP to its firewall), then re-run
   `azd provision`. Requires permission to change those network settings.
3. **Add private networking** — VNet + private endpoint for Search, and run
   the setup from inside the VNet. Most secure but out of scope for the
   quick-start.

> Also required (all paths): the deployer needs the data-plane roles the
> templates assign — Search Index Data Contributor + Search Service
> Contributor and Storage Blob Data Contributor. Azure data-plane RBAC can
> take a few minutes to propagate; `postprovision` retries automatically.
> If it still reports "unauthorized after N attempts," wait a couple minutes
> and re-run `azd provision`.

## Clean up

Delete all provisioned resources (stops all charges):

```powershell
azd down --purge
```

`--purge` also purges soft-deleted Key Vault / Cognitive Services resources so
the names can be reused immediately.

## Repository structure

```
azure.yaml               # azd manifest: agents + Container Apps + hooks
ARCHITECTURE.md          # source of truth — components, data flows, contracts
pyproject.toml           # Python 3.12 monorepo + per-package extras
README.md                # this guide
SECURITY.md              # POC boundary and production hardening requirements

infra/                   # Bicep IaC (subscription-scoped)
  main.bicep             #   RG + module wiring + outputs contract
  main.parameters.json   #   azd env -> Bicep param binding
  abbreviations.json     #   resource-name abbreviations
  modules/               #   monitoring, identity, keyvault, storage, search,
                         #   foundry (models), cosmos, container-apps, doc-intel

agents/                  # Hosted Agent source (Python 3.13 remote build)
  coordinator/           #   research-coordinator
  literature/            #   literature-agent (+ literature-online-agent)
  grant/                 #   grant-agent (+ grant-online-agent)
  matching/              #   matching-agent (+ matching-online-agent)
  dataset/               #   dataset-agent
  institution/           #   institution-agent
  shared/                #   shared config, credential helpers, evidence contracts
  evals/                 #   evaluation datasets and eval YAML manifests

packages/
  research_core/         #   evidence, citation, approval, and score contracts
  research_connectors/   #   PubMed, Europe PMC, Crossref, OpenAlex, arXiv, etc.

services/
  api/                   #   FastAPI research API (Container App)
  worker/                #   ingestion background worker
  connector_adapter/     #   connector proxy + rate-limit adapter

apps/
  web/                   #   Next.js workbench (Container App)

scripts/                 #   azd lifecycle hooks, service reconciliation, agent RBAC
tests/                   #   live pytest release checks
```

| Directory | Azure resources owned | Responsibility |
|-----------|----------------------|----------------|
| `infra/` | All Azure resources (Bicep) | Infrastructure |
| `agents/` | Nine Foundry Hosted Agents | Agent logic + evidence contracts |
| `services/api/` | FastAPI Container App | Research API, ingestion, approval gate |
| `apps/web/` | Next.js Container App | Workbench UI |
| `packages/` | — | Shared Python contracts and connectors |
| `scripts/` | azd lifecycle hooks | Postprovision: index and live service reconciliation |
| `tests/` | — | Quality gate: pytest + Playwright |

## Data and safety

The included corpus is synthetic and CC0. Do not replace it with institutional
or regulated data until access control, retention, network isolation, data
residency, authentication, and compliance requirements are approved.

See [SECURITY.md](SECURITY.md) for the POC boundary and production hardening
requirements.

## License

MIT — see `pyproject.toml`.

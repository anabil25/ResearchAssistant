"""Generate the APIM-native normalized connector OpenAPI document."""

# ruff: noqa: E501

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from research_assistant_core.connector_catalog import connector_definitions

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "infra" / "provider-specs" / "authored" / "research_connectors.json"
POLICY_OUTPUT = ROOT / "infra" / "connector-operation-policies.json"
NOTICE = (
    "Metadata only. Verify source rights, current status, and full text before using a record as research evidence."
)

PROVIDERS: dict[str, dict[str, str]] = {
    "europe_pmc": {
        "base": "https://www.ebi.ac.uk/europepmc/webservices/rest",
        "search_path": "/search",
        "search_selector": 'payload["resultList"]?["result"]',
        "terms": "https://europepmc.org/terms",
        "retrieved": "https://www.ebi.ac.uk/europepmc/webservices/rest/",
    },
    "crossref": {
        "base": "https://api.crossref.org",
        "search_path": "/works",
        "search_selector": 'payload["message"]?["items"]',
        "terms": "https://www.crossref.org/terms/",
        "retrieved": "https://api.crossref.org/works",
    },
    "openalex": {
        "base": "https://api.openalex.org",
        "search_path": "/works",
        "search_selector": 'payload["results"]',
        "terms": "https://openalex.org/terms",
        "retrieved": "https://api.openalex.org/works",
    },
    "arxiv": {
        "base": "https://export.arxiv.org",
        "search_path": "/api/query",
        "search_selector": 'payload["feed"]?["entry"]',
        "terms": "https://info.arxiv.org/help/api/tou.html",
        "retrieved": "https://export.arxiv.org/api/query",
    },
    "clinical_trials": {
        "base": "https://clinicaltrials.gov",
        "search_path": "/api/v2/studies",
        "search_selector": 'payload["studies"]',
        "terms": "https://clinicaltrials.gov/about-site/terms-conditions",
        "retrieved": "https://clinicaltrials.gov/api/v2/studies",
    },
    "grants_gov": {
        "base": "https://api.grants.gov",
        "search_path": "/v1/api/search2",
        "search_selector": 'payload["data"]?["oppHits"]',
        "terms": "https://www.grants.gov/web/grants/legal.html",
        "retrieved": "https://api.grants.gov/v1/api/search2",
    },
    "nih_reporter": {
        "base": "https://api.reporter.nih.gov",
        "search_path": "/v2/projects/search",
        "search_selector": 'payload["results"]',
        "terms": "https://reporter.nih.gov/about",
        "retrieved": "https://api.reporter.nih.gov/v2/projects/search",
    },
    "datacite": {
        "base": "https://api.datacite.org",
        "search_path": "/dois",
        "search_selector": 'payload["data"]',
        "terms": "https://datacite.org/terms.html",
        "retrieved": "https://api.datacite.org/dois",
    },
    "orcid": {
        "base": "https://pub.orcid.org",
        "search_path": "/v3.0/expanded-search/",
        "search_selector": 'payload["expanded-result"]',
        "terms": "https://info.orcid.org/terms-of-use/",
        "retrieved": "https://pub.orcid.org/v3.0/expanded-search/",
    },
    "ror": {
        "base": "https://api.ror.org",
        "search_path": "/v2/organizations",
        "search_selector": 'payload["items"]',
        "terms": "https://ror.org/terms/",
        "retrieved": "https://api.ror.org/v2/organizations",
    },
    "semantic_scholar": {
        "base": "https://api.semanticscholar.org",
        "search_path": "/graph/v1/paper/search",
        "search_selector": 'payload["data"]',
        "terms": "https://www.semanticscholar.org/product/api/license",
        "retrieved": "https://api.semanticscholar.org/graph/v1/paper/search",
    },
}


def _response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "source",
            "query",
            "records",
            "terms_url",
            "retrieved_from",
            "warnings",
            "notice",
        ],
        "properties": {
            "source": {"type": "string"},
            "query": {"type": "string"},
            "records": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "terms_url": {"type": "string", "format": "uri"},
            "retrieved_from": {"type": "string", "format": "uri"},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "notice": {"type": "string"},
        },
    }


def _responses() -> dict[str, Any]:
    return {
        "200": {
            "description": "Normalized public metadata evidence.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ConnectorResult"}
                }
            },
        },
        "422": {"description": "Invalid connector input."},
        "502": {"description": "Upstream provider unavailable."},
    }


def connector_apim_openapi() -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for connector in connector_definitions():
        for operation in connector.operations:
            if operation.operation_class == "delete":
                continue
            if operation.mcp_tool_name == "search":
                path = f"/v1/connectors/{connector.id}/search"
                parameters = [
                    {
                        "name": "query",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string", "minLength": 2, "maxLength": 500},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "default": 5,
                        },
                    },
                ]
            elif operation.mcp_tool_name == "lookup":
                path = f"/v1/connectors/{connector.id}/lookup"
                parameters = [
                    {
                        "name": "identifier",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1, "maxLength": 255},
                    }
                ]
            else:
                raise ValueError(
                    f"Unsupported normalized connector operation: {connector.id}/{operation.mcp_tool_name}"
                )
            paths[path] = {
                "get": {
                    "operationId": operation.id,
                    "summary": f"{connector.name} {operation.mcp_tool_name}",
                    "description": connector.description,
                    "parameters": parameters,
                    "responses": _responses(),
                    "tags": [connector.id],
                }
            }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Research Assistant normalized connectors",
            "version": "1.0.0",
            "description": (
                "Bounded deterministic normalization for public research metadata providers."
            ),
        },
        "servers": [{"url": "https://normalized-connectors.invalid"}],
        "paths": paths,
        "components": {"schemas": {"ConnectorResult": _response_schema()}},
    }


def _policy_expression(code: str) -> str:
    return escape(code.strip(), quote=False)


def _set_variable(name: str, query_name: str, default: str) -> str:
    return (
        f'<set-variable name="{name}" '
        f'value="@(context.Request.Url.Query.GetValueOrDefault(&quot;{query_name}&quot;, &quot;{default}&quot;))" />'
    )


def _set_query(name: str, expression: str) -> str:
    return (
        f'<set-query-parameter name="{name}" exists-action="override">'
        f"<value>{_policy_expression(expression)}</value>"
        "</set-query-parameter>"
    )


def _normalize_body(
    *,
    source: str,
    query_variable: str,
    selector: str,
    terms_url: str,
    retrieved_from: str,
    payload_expression: str = "context.Response.Body.As<JObject>()",
    warnings_expression: str = "new JArray()",
) -> str:
    code = f"""
@{{
    var payload = {payload_expression};
    JToken selected = {selector};
    var records = selected == null
        ? new JArray()
        : selected is JArray ? (JArray)selected : new JArray(selected);
    var bounded = new JArray();
    var limit = Int32.Parse((string)context.Variables["normalizedLimit"]);
    foreach (var item in records) {{
        if (bounded.Count >= limit) {{
            break;
        }}
        bounded.Add(item);
    }}
    return new JObject(
        new JProperty("source", "{source}"),
        new JProperty("query", (string)context.Variables["{query_variable}"]),
        new JProperty("records", bounded),
        new JProperty("terms_url", "{terms_url}"),
        new JProperty("retrieved_from", "{retrieved_from}"),
        new JProperty("warnings", {warnings_expression}),
        new JProperty("notice", "{NOTICE}")
    ).ToString();
}}
"""
    return (
        '<set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header>'
        f"<set-body>{_policy_expression(code)}</set-body>"
    )


def _policy(inbound: list[str], outbound: list[str]) -> str:
    value = (
        "<policies><inbound><base />"
        + "".join(inbound)
        + "</inbound><backend><base /></backend><outbound><base />"
        + "".join(outbound)
        + "</outbound><on-error><base /></on-error></policies>"
    )
    ElementTree.fromstring(value)
    return value


def _common_inbound(*, lookup: bool = False) -> list[str]:
    result = [
        _set_variable(
            "normalizedIdentifier" if lookup else "normalizedQuery",
            "identifier" if lookup else "query",
            "",
        ),
        _set_variable("normalizedLimit", "limit", "1" if lookup else "5"),
        '<set-header name="Authorization" exists-action="delete" />',
    ]
    return result


def _simple_search_policy(source: str) -> str:
    provider = PROVIDERS[source]
    inbound = _common_inbound()
    inbound.extend(
        [
            f'<set-backend-service base-url="{provider["base"]}" />',
            f'<rewrite-uri template="{provider["search_path"]}" copy-unmatched-params="false" />',
        ]
    )
    query_parameters: dict[str, str] = {
        "europe_pmc": {"query": '@((string)context.Variables["normalizedQuery"])', "format": "json", "pageSize": '@((string)context.Variables["normalizedLimit"])'},
        "crossref": {"query.bibliographic": '@((string)context.Variables["normalizedQuery"])', "rows": '@((string)context.Variables["normalizedLimit"])', "mailto": "{{research-connector-contact}}"},
        "openalex": {"search": '@((string)context.Variables["normalizedQuery"])', "per-page": '@((string)context.Variables["normalizedLimit"])', "mailto": "{{research-connector-contact}}"},
        "arxiv": {"search_query": '@("all:" + (string)context.Variables["normalizedQuery"])', "start": "0", "max_results": '@((string)context.Variables["normalizedLimit"])'},
        "clinical_trials": {"query.term": '@((string)context.Variables["normalizedQuery"])', "pageSize": '@((string)context.Variables["normalizedLimit"])', "format": "json"},
        "datacite": {"query": '@((string)context.Variables["normalizedQuery"])', "page[size]": '@((string)context.Variables["normalizedLimit"])'},
        "orcid": {"q": '@((string)context.Variables["normalizedQuery"])', "rows": '@((string)context.Variables["normalizedLimit"])'},
        "ror": {"query": '@((string)context.Variables["normalizedQuery"])', "page": "1"},
        "semantic_scholar": {"query": '@((string)context.Variables["normalizedQuery"])', "limit": '@((string)context.Variables["normalizedLimit"])', "fields": "title,authors,year,externalIds,citationCount,url,isOpenAccess"},
    }.get(source, {})
    for name, expression in query_parameters.items():
        inbound.append(_set_query(name, expression))
    if source in {"europe_pmc", "orcid"}:
        inbound.append('<set-header name="Accept" exists-action="override"><value>application/json</value></set-header>')
    if source == "arxiv":
        inbound.append('<set-header name="Accept" exists-action="override"><value>application/atom+xml</value></set-header>')
    if source in {"grants_gov", "nih_reporter"}:
        inbound.extend(
            [
                "<set-method>POST</set-method>",
                '<set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header>',
            ]
        )
        body = (
            '@{ return new JObject(new JProperty("keyword", (string)context.Variables["normalizedQuery"]), '
            'new JProperty("rows", Int32.Parse((string)context.Variables["normalizedLimit"])), '
            'new JProperty("oppStatuses", "forecasted|posted")).ToString(); }'
            if source == "grants_gov"
            else '@{ return new JObject(new JProperty("criteria", new JObject(new JProperty("advanced_text_search", '
            'new JObject(new JProperty("operator", "and"), new JProperty("search_field", "all"), '
            'new JProperty("search_text", (string)context.Variables["normalizedQuery"]))))), '
            'new JProperty("offset", 0), new JProperty("limit", Int32.Parse((string)context.Variables["normalizedLimit"]))).ToString(); }'
        )
        inbound.append(f"<set-body>{_policy_expression(body)}</set-body>")
    outbound: list[str] = []
    if source == "arxiv":
        outbound.append(
            '<xml-to-json kind="direct" apply="always" consider-accept-header="false" always-array-child-elements="true" />'
        )
    if source == "semantic_scholar":
        warning_body = _normalize_body(
            source=source,
            query_variable="normalizedQuery",
            selector="null",
            terms_url=provider["terms"],
            retrieved_from=provider["retrieved"],
            payload_expression="new JObject()",
            warnings_expression='new JArray("Anonymous Semantic Scholar quota is exhausted. Configure an approved API key in APIM.")',
        )
        normal_body = _normalize_body(
            source=source,
            query_variable="normalizedQuery",
            selector=provider["search_selector"],
            terms_url=provider["terms"],
            retrieved_from=provider["retrieved"],
        )
        outbound.append(
            '<choose><when condition="@(context.Response.StatusCode == 429)">'
            '<set-status code="200" reason="OK" />'
            f"{warning_body}</when><otherwise>{normal_body}</otherwise></choose>"
        )
    else:
        outbound.append(
            _normalize_body(
                source=source,
                query_variable="normalizedQuery",
                selector=provider["search_selector"],
                terms_url=provider["terms"],
                retrieved_from=provider["retrieved"],
            )
        )
    return _policy(inbound, outbound)


def _pubmed_policy(*, lookup: bool) -> str:
    inbound = _common_inbound(lookup=lookup)
    inbound.extend(
        [
            '<set-backend-service base-url="https://eutils.ncbi.nlm.nih.gov" />',
            (
                '<rewrite-uri template="/entrez/eutils/esummary.fcgi" copy-unmatched-params="false" />'
                if lookup
                else '<rewrite-uri template="/entrez/eutils/esearch.fcgi" copy-unmatched-params="false" />'
            ),
            _set_query("db", "pubmed"),
            _set_query("retmode", "json"),
            _set_query("tool", "research-assistant"),
            _set_query("email", "{{research-connector-contact}}"),
            _set_query(
                "id" if lookup else "term",
                '@((string)context.Variables["normalizedIdentifier"])'
                if lookup
                else '@((string)context.Variables["normalizedQuery"])',
            ),
        ]
    )
    if not lookup:
        inbound.append(_set_query("retmax", '@((string)context.Variables["normalizedLimit"])'))
    if lookup:
        payload_expression = "context.Response.Body.As<JObject>()"
    else:
        payload_expression = '((IResponse)context.Variables["pubmedSummary"]).Body.As<JObject>()'
    body = _normalize_body(
        source="pubmed",
        query_variable="normalizedIdentifier" if lookup else "normalizedQuery",
        selector='payload["result"]',
        terms_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
        retrieved_from="https://eutils.ncbi.nlm.nih.gov/",
        payload_expression=payload_expression,
    )
    outbound: list[str] = []
    if not lookup:
        outbound.extend(
            [
                '<set-variable name="pubmedIds" value="@{ var search = context.Response.Body.As&lt;JObject&gt;(preserveContent: true); var ids = search[&quot;esearchresult&quot;]?[&quot;idlist&quot;] as JArray; return ids == null ? &quot;&quot; : String.Join(&quot;,&quot;, ids.Select(item => (string)item)); }" />',
                '<send-request mode="new" response-variable-name="pubmedSummary" timeout="20" ignore-error="false">'
                '<set-url>@("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&amp;retmode=json&amp;tool=research-assistant&amp;email={{research-connector-contact}}&amp;id=" + (string)context.Variables["pubmedIds"])</set-url>'
                '<set-method>GET</set-method>'
                '</send-request>',
            ]
        )
    outbound.append(body)
    return _policy(inbound, outbound)


def _lookup_policy(source: str) -> str:
    provider = PROVIDERS[source]
    inbound = _common_inbound(lookup=True)
    path_expressions = {
        "europe_pmc": '@{ var value = (string)context.Variables["normalizedIdentifier"]; var separator = value.IndexOf(":"); return "/article/" + Uri.EscapeDataString(value.Substring(0, separator)) + "/" + Uri.EscapeDataString(value.Substring(separator + 1)); }',
        "crossref": '@("/works/" + Uri.EscapeDataString((string)context.Variables["normalizedIdentifier"]))',
        "openalex": '@("/works/" + Uri.EscapeDataString((string)context.Variables["normalizedIdentifier"]))',
        "clinical_trials": '@("/api/v2/studies/" + Uri.EscapeDataString((string)context.Variables["normalizedIdentifier"]))',
        "datacite": '@("/dois/" + Uri.EscapeDataString((string)context.Variables["normalizedIdentifier"]))',
        "ror": '@("/v2/organizations/" + Uri.EscapeDataString((string)context.Variables["normalizedIdentifier"]))',
        "arxiv": "/api/query",
    }
    inbound.extend(
        [
            f'<set-backend-service base-url="{provider["base"]}" />',
            f'<rewrite-uri template="{escape(path_expressions[source], quote=True)}" copy-unmatched-params="false" />',
        ]
    )
    if source == "arxiv":
        inbound.extend(
            [
                _set_query("id_list", '@((string)context.Variables["normalizedIdentifier"])'),
                _set_query("max_results", "1"),
                '<set-header name="Accept" exists-action="override"><value>application/atom+xml</value></set-header>',
            ]
        )
    selectors = {
        "europe_pmc": "payload",
        "crossref": 'payload["message"]',
        "openalex": "payload",
        "clinical_trials": "payload",
        "datacite": 'payload["data"]',
        "ror": "payload",
        "arxiv": 'payload["feed"]?["entry"]',
    }
    outbound: list[str] = []
    if source == "arxiv":
        outbound.append(
            '<xml-to-json kind="direct" apply="always" consider-accept-header="false" always-array-child-elements="true" />'
        )
    outbound.append(
        _normalize_body(
            source=source,
            query_variable="normalizedIdentifier",
            selector=selectors[source],
            terms_url=provider["terms"],
            retrieved_from=provider["retrieved"],
        )
    )
    return _policy(inbound, outbound)


def connector_operation_policies() -> list[dict[str, str]]:
    policies: dict[str, str] = {"pubmedSearch": _pubmed_policy(lookup=False)}
    for source in PROVIDERS:
        policies[f"{source.split('_')[0]}{''.join(part.capitalize() for part in source.split('_')[1:])}Search"] = (
            _simple_search_policy(source)
        )
    policies["pubmedLookup"] = _pubmed_policy(lookup=True)
    for source in (
        "europe_pmc",
        "crossref",
        "openalex",
        "arxiv",
        "clinical_trials",
        "datacite",
        "ror",
    ):
        operation = next(
            operation
            for connector in connector_definitions()
            if connector.id == source
            for operation in connector.operations
            if operation.mcp_tool_name == "lookup"
        )
        policies[operation.id] = _lookup_policy(source)
    expected = {
        operation.id
        for connector in connector_definitions()
        for operation in connector.operations
        if operation.operation_class != "delete"
    }
    if set(policies) != expected:
        raise ValueError(
            f"Connector APIM policy coverage mismatch: missing={sorted(expected - set(policies))}, "
            f"unexpected={sorted(set(policies) - expected)}"
        )
    return [
        {"operationId": operation_id, "value": policies[operation_id]}
        for operation_id in sorted(policies)
    ]


def main() -> None:
    document = connector_apim_openapi()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT.relative_to(ROOT)} with {len(document['paths'])} operations.")
    policies = connector_operation_policies()
    POLICY_OUTPUT.write_text(
        json.dumps(policies, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {POLICY_OUTPUT.relative_to(ROOT)} with {len(policies)} policies.")


if __name__ == "__main__":
    main()
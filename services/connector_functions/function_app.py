from __future__ import annotations

import json
import logging
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from xml.etree.ElementTree import ParseError

import azure.functions as func
import httpx
from research_assistant_connectors import ResearchConnectorRegistry

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
RegistryFactory = Callable[[], ResearchConnectorRegistry]
registry_factory: RegistryFactory = ResearchConnectorRegistry
logger = logging.getLogger(__name__)


def _json_response(payload: dict[str, object], status_code: int) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, separators=(",", ":")),
        status_code=status_code,
        mimetype="application/json",
    )


def _limit(request: func.HttpRequest) -> int:
    raw = request.params.get("limit", "5")
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError("Connector limit must be an integer") from exc


def _invoke(
    request: func.HttpRequest,
    operation: Callable[[ResearchConnectorRegistry], object],
) -> func.HttpResponse:
    registry = registry_factory()
    try:
        result = operation(registry)
        return func.HttpResponse(result.to_json(), status_code=200, mimetype="application/json")
    except ValueError as exc:
        return _json_response({"detail": str(exc)}, 422)
    except (
        httpx.HTTPError,
        HTTPError,
        URLError,
        OSError,
        TimeoutError,
        json.JSONDecodeError,
        ParseError,
    ) as exc:
        logger.warning("Connector invocation failed: %s", exc)
        return _json_response({"detail": "Connector provider is unavailable."}, 502)
    finally:
        registry.close()


@app.function_name(name="connector_search")
@app.route(route="v1/connectors/{source}/search", methods=["GET"])
def connector_search(request: func.HttpRequest) -> func.HttpResponse:
    source = request.route_params.get("source", "")
    query = request.params.get("query", "")
    try:
        limit = _limit(request)
    except ValueError as exc:
        return _json_response({"detail": str(exc)}, 422)
    return _invoke(
        request,
        lambda registry: registry.search(source, query, limit=limit),
    )


@app.function_name(name="connector_lookup")
@app.route(route="v1/connectors/{source}/lookup", methods=["GET"])
def connector_lookup(request: func.HttpRequest) -> func.HttpResponse:
    source = request.route_params.get("source", "")
    identifier = request.params.get("identifier", "")
    return _invoke(
        request,
        lambda registry: registry.lookup(source, identifier),
    )
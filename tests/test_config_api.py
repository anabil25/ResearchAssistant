from __future__ import annotations

import pytest
from pydantic import ValidationError
from research_assistant_api.config import Settings


@pytest.mark.parametrize(
    ("field", "error_message"),
    [
        ("foundry_project_endpoint", "Foundry project endpoint must use HTTPS"),
        ("cosmos_endpoint", "Cosmos DB endpoint must use HTTPS"),
        ("storage_blob_endpoint", "Storage Blob endpoint must use HTTPS"),
        ("search_endpoint", "Azure AI Search endpoint must use HTTPS"),
    ],
)
def test_https_validators_strip_trailing_slash_and_reject_http(
    field: str,
    error_message: str,
) -> None:
    stripped = Settings(**{field: "https://example.test/"})  # type: ignore[arg-type]
    empty = Settings(**{field: None})  # type: ignore[arg-type]

    assert getattr(stripped, field) == "https://example.test"
    assert getattr(empty, field) is None
    with pytest.raises(ValidationError, match=error_message):
        Settings(**{field: "http://example.test"})  # type: ignore[arg-type]


def test_allowed_origins_accepts_strings_and_lists() -> None:
    parsed = Settings(allowed_origins=" https://one.test , http://two.test ,, ")  # type: ignore[arg-type]
    passthrough = Settings(allowed_origins=["https://already.test"])

    assert parsed.allowed_origins == ["https://one.test", "http://two.test"]
    assert passthrough.allowed_origins == ["https://already.test"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("https://gateway.example.test/", "https://gateway.example.test"),
        ("http://localhost:8080/", "http://localhost:8080"),
        ("http://127.0.0.1:9000/path/", "http://127.0.0.1:9000/path"),
    ],
)
def test_connector_gateway_url_accepts_supported_values(
    value: str | None,
    expected: str | None,
) -> None:
    settings = Settings(connector_gateway_url=value)

    assert settings.connector_gateway_url == expected


@pytest.mark.parametrize(
    ("value", "error_message"),
    [
        (
            "http://gateway.example.test",
            "Connector gateway URL must use HTTPS except for local loopback development",
        ),
        (
            "https://user:pass@gateway.example.test",
            "Connector gateway URL must not contain credentials, query, or fragment",
        ),
        (
            "https://gateway.example.test?token=secret",
            "Connector gateway URL must not contain credentials, query, or fragment",
        ),
        (
            "https://gateway.example.test#fragment",
            "Connector gateway URL must not contain credentials, query, or fragment",
        ),
    ],
)
def test_connector_gateway_url_rejects_invalid_values(
    value: str,
    error_message: str,
) -> None:
    with pytest.raises(ValidationError, match=error_message):
        Settings(connector_gateway_url=value)

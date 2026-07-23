def main() -> None:
    import uvicorn

    uvicorn.run(
        "research_assistant_connector_adapter.app:app",
        host="0.0.0.0",
        port=8200,
    )


__all__ = ["main"]

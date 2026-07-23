import uvicorn

from research_assistant_api.app import app

__all__ = ["app"]


def main() -> None:
    uvicorn.run(
        "research_assistant_api.app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )

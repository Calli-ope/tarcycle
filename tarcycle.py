"""Compatibility entry point for the Tarcycle backend."""

from backend.app import app
from backend.router import NetworkXRouter

__all__ = ["NetworkXRouter", "app"]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app:app", host="127.0.0.1", port=8000, reload=True)

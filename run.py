"""Convenience launcher: loads .env then runs uvicorn.

Windows: from the project root run `python run.py`, then open
http://127.0.0.1:8000
"""

from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (same dir as this file) before app import.
load_dotenv(Path(__file__).parent / ".env")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )

"""Local dev entrypoint. In production use run.ps1 / launch_desktop.vbs."""
import os
from pathlib import Path

# Load .env config (TDX path, FFD, QMT, etc.) before app imports
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import uvicorn
from app.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")

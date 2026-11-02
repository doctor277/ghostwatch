from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = DATA_DIR / "ghostwatch.db"
HOST = "127.0.0.1"
PORT = 8765
MAX_HISTORY_SAMPLES = 300
MAX_CONNECTIONS = 250
MAX_PROCESSES = 200
MAX_LIVE_ALERTS = 200
MAX_WEBSOCKET_CLIENTS = 32
WEBSOCKET_SEND_TIMEOUT_SECONDS = 2.0
ALLOWED_HTTP_HOSTS = frozenset(
    {
        f"127.0.0.1:{PORT}",
        f"localhost:{PORT}",
        f"[::1]:{PORT}",
    }
)
ALLOWED_WEBSOCKET_ORIGINS = frozenset(
    {
        f"http://127.0.0.1:{PORT}",
        f"http://localhost:{PORT}",
        f"http://[::1]:{PORT}",
    }
)

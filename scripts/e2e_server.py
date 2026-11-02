from __future__ import annotations

from pathlib import Path

from app.main import create_app

app = create_app(Path("data/e2e-tests.db"), start_background=False)

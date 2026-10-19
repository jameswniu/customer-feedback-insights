"""Start the real API with the model endpoint pointed at a local stand-in (used by the edge cases that fake a rejected key and a made-up model quote).

Only nlp.API_URL is overridden, so the whole pipeline, verification and insights code is the real code. Nothing under app/ is edited.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from app.analyze import nlp  # noqa: E402

if os.environ.get("EVALS_FAKE_API_URL"):
    nlp.API_URL = os.environ["EVALS_FAKE_API_URL"]

from app.main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=int(os.environ["PORT"]), log_level="warning")

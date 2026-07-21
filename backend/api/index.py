"""
Vercel serverless entrypoint.

Vercel looks for an `api/` directory and serves an ASGI app exported as `app`.
vercel.json rewrites every path here, so FastAPI still owns its own routing
and /docs, /health and /api/v1/* work exactly as they do locally.
"""

import os
import sys

# The function runs with api/ as its directory; the application package lives
# one level up in backend/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

__all__ = ["app"]

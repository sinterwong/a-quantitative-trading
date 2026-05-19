"""HTTP route modules. R2-4: incrementally split backend/api.py (1830 行) into
Flask Blueprints, one resource per file.

Each module exposes a single ``<name>_bp`` Blueprint. ``backend/api.py``
imports them at the end of its module body and calls
``app.register_blueprint(...)``. This means:

- Shared helpers (``rate_limit``, ``ok``, ``err``, ``get_svc``,
  ``_get_or_build_broker``, etc.) stay in ``backend.api`` and route modules
  ``from backend.api import ...`` them. Python's module cache makes this
  safe because the helpers are defined *before* the bottom-of-file
  ``register_blueprint`` lines.
- Route modules MUST NOT execute Flask side effects at import time other
  than building their own Blueprint. Anything else belongs in ``backend.api``.
"""

"""Test-session environment policy.

``Settings.allow_demo_identity`` defaults to ``False`` in application code
(the unauthenticated "demo sandbox" identity bypass must be an explicit,
impossible-to-misconfigure-into-production local/dev/test adapter, never a
silent default -- see the review finding this fixes). Most of this test
suite exercises endpoints without supplying an authenticated principal and
therefore *does* need the demo identity enabled; that opt-in must happen
here, as an explicit environment-variable declaration for the test session,
rather than by relying on the library default. This module is collected
before any test module in this directory imports
``research_assistant_api.app`` (whose module-level ``app.state.settings``
is resolved once via the process-wide ``get_settings()`` cache), so setting
the environment variable here reliably takes effect for that first import.

``os.environ.setdefault`` is used (not ``setenv``) so a test that needs to
exercise the "demo identity disabled" behavior can still monkeypatch this
variable to ``"false"`` for its own scope without being overridden back.
"""

from __future__ import annotations

import os

os.environ.setdefault("RESEARCH_ALLOW_DEMO_IDENTITY", "true")

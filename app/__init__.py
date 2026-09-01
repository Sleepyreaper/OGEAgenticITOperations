"""Reusable multi-agent Azure operations platform — Flask application package.

Branding, agent names/models, and system prompts are profile-driven; see
app/config.py and the profiles/ directory. "power" (the default) is a
generic power-utility reference deployment on GPT-5.6 Sol/Terra/Luna (see
docs/MODEL_CONFIGURATION.md); "oge" reproduces this app's original
hardcoded behavior exactly and remains checked in as a selectable
legacy/example profile.

Note: this file (and app/profiles.py) intentionally have zero third-party
dependencies, so `scripts/configure.py` (stdlib-only) can safely import
app.profiles without requiring anything from requirements.txt to be
installed first. Third-party setup (e.g. loading a local .env file) lives
in app/config.py instead, since only that module actually needs it.
"""

__version__ = "1.2.0"
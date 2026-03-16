"""WSGI entry point for gunicorn / Azure App Service."""

import os

from app.main import create_app

app = create_app()

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=debug)
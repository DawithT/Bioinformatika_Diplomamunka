"""
run.py — Development server entry point.

Production: use a proper WSGI server (gunicorn, waitress).
This file is for local development only.
"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True,
        use_reloader=True,
    )

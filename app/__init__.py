"""
app/__init__.py — Flask application factory.

Usage:
    from app import create_app
    app = create_app()              # uses config.settings
    app = create_app(testing=True)  # skips Redis for unit tests

Important: keep the module-level scope free of third-party imports.
app/domain/* must be importable with stdlib only (for fast unit tests).
All heavy imports (Flask, structlog, Redis) live inside create_app().
"""
from __future__ import annotations


def create_app(testing: bool = False):
    """
    Create and configure the Flask application.

    Steps:
    1. Configure structlog (must be first — other modules use it at import time)
    2. Validate settings
    3. Create Flask app and push config
    4. Initialise extensions (Redis/RQ)
    5. Load the field registry (validates Registry_base.xlsx)
    6. Register blueprints
    """
    # All third-party imports are local to keep the module importable with
    # stdlib only — this matters for fast unit tests of domain models.
    import structlog
    from flask import Flask
    from config import settings
    from app.extensions import configure_logging, init_extensions

    # 1. Logging — configure before anything else logs
    configure_logging(log_level="DEBUG" if settings.flask_env == "development" else "INFO")
    log = structlog.get_logger(__name__)

    app = Flask(__name__, template_folder="templates", static_folder="static")

    # 2. Flask config from settings
    app.config.update(settings.to_flask_config())
    if testing:
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

    # 3. Extensions
    if not testing:
        try:
            init_extensions(settings.redis_url)
            log.info("redis_connected", url=settings.redis_url)
        except Exception as exc:
            # Fail loud: the worker queue is essential, not optional.
            raise RuntimeError(
                f"Cannot connect to Redis at {settings.redis_url!r}. "
                "Start Redis with: redis-server\n"
                f"Original error: {exc}"
            ) from exc

    # 4. Registry — load and validate at startup
    # (import here to avoid circular; registry module uses settings at module level)
    from app.registry.loader import load_registry, RegistryValidationError

    try:
        registry = load_registry(settings.registry_path_obj)
        app.config["FIELD_REGISTRY"] = registry
        log.info("registry_loaded", field_count=len(registry))
    except RegistryValidationError as exc:
        raise RuntimeError(f"Registry validation failed:\n{exc}") from exc
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Registry file not found: {settings.registry_path_obj.resolve()}\n"
            "Place Registry_base.xlsx in the config/ directory."
        ) from exc

    # 5. Blueprints
    from app.blueprints.main import bp as main_bp
    app.register_blueprint(main_bp)

    log.info(
        "app_created",
        env=settings.flask_env,
        llm=settings.llm_provider,
        llm_enabled=settings.enable_llm,
    )
    return app

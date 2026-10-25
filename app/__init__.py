# Пакет app для обработки документов

def create_app(config_class=None):
    """Создает экземпляр Flask приложения."""
    import secrets
    from flask import Flask, abort, request, session
    from app.config import Config as DefaultConfig
    from app.database import close_db, init_db
    from app.web.routes import bp as web_bp
    
    if config_class is None:
        config_class = DefaultConfig
    
    app = Flask(__name__)
    app.config.from_object(config_class)

    config_class.ensure_directories()

    with app.app_context():
        init_db()

    app.teardown_appcontext(close_db)
    app.register_blueprint(web_bp)

    @app.context_processor
    def inject_csrf_token():
        def csrf_token():
            token = session.get("_csrf_token")
            if token is None:
                token = secrets.token_urlsafe(32)
                session["_csrf_token"] = token
            return token
        return {"csrf_token": csrf_token}

    @app.before_request
    def protect_mutations():
        if request.method == "POST":
            expected = session.get("_csrf_token")
            supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not expected or not supplied or not secrets.compare_digest(expected, supplied):
                abort(400, description="CSRF token is missing or invalid")

    @app.cli.command("expiry-report")
    def expiry_report():
        """Print expired and soon-to-expire documents for scheduled checks."""
        from flask import current_app
        from app.repositories import DocumentRepository
        from app.services.expiry import expiry_state

        rows = DocumentRepository().list_all()
        alerts = [
            (row, expiry_state(row, current_app.config["EXPIRY_WARNING_DAYS"]))
            for row in rows
        ]
        alerts = [(row, state) for row, state in alerts if state["key"] in {"expired", "expiring"}]
        if not alerts:
            print("Нет просроченных документов и документов, срок которых скоро истекает.")
            return
        for row, state in alerts:
            print(f"{row['contract_number']} | {row['object_name']} | {row['working_title'] or row['original_filename']} | {state['label']}")

    return app

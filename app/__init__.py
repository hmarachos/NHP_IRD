# Пакет app для обработки документов

def create_app(config_class=None):
    """Создает экземпляр Flask приложения."""
    from flask import Flask
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
    return app
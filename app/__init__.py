from flask import Flask

from app.config import Config
from app.database import close_db, init_db
from app.web.routes import bp as web_bp


def create_app(config_class: type[Config] = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    config_class.ensure_directories()

    with app.app_context():
        init_db()

    app.teardown_appcontext(close_db)
    app.register_blueprint(web_bp)
    return app

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from .auth import bp as auth_bp
from .cases import bp as cases_bp
from .db import init_db
from .errors import ApiError


def create_app():
    app = Flask(__name__)
    init_db()
    app.register_blueprint(auth_bp)
    app.register_blueprint(cases_bp)

    @app.errorhandler(ApiError)
    def api_error(exc):
        return jsonify(error=exc.message), exc.status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    return app

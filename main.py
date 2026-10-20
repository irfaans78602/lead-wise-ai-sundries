import os
from flask import Flask, jsonify
from flask_cors import CORS
from ask_ai import bp as ask_ai_bp

def create_app():
    app = Flask(__name__)
    # Broad CORS to allow requests from any tenant domain. 
    # Security is enforced per-request in ask_ai.py based on allowed_origins.
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    app.register_blueprint(ask_ai_bp)

    @app.get("/health")
    def health():
        return jsonify({"ok": True}), 200

    return app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
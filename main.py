import os
from flask import Flask, jsonify
from flask_cors import CORS
from contact_log import bp as contact_log_bp
from ask_ai import bp as ask_ai_bp
from auth import bp as auth_bp
from embedded_signup import bp as embedded_signup_bp  # <--- NEW IMPORT

def create_app():
    app = Flask(__name__)
    
    # Allow requests from production and local development
    CORS(app, resources={
        r"/api/*": {
            "origins": [
                "https://lead-wise-ai.com",
                "https://www.lead-wise-ai.com",
                "http://localhost:8080",
                "http://127.0.0.1:8080",
                "http://localhost:3000"
            ]
        }
    })
    
    # Setup sessions for auth.py
    secret = os.environ.get("SESSION_SECRET")
    if not secret:
        raise RuntimeError("SESSION_SECRET is not set.")
    app.config["SECRET_KEY"] = secret
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    app.register_blueprint(contact_log_bp)
    app.register_blueprint(ask_ai_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(embedded_signup_bp)  # <--- NEW REGISTRATION

    @app.get("/health")
    def health():
        return jsonify({"ok": True}), 200

    return app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
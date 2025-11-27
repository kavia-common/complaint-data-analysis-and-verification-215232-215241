from flask import Flask
from flask_cors import CORS
from flask_smorest import Api
from .routes.health import blp as health_blp
from .routes.complaints import blp_complaints

app = Flask(__name__)
app.url_map.strict_slashes = False

# Enable permissive CORS (adjust origin via env if needed)
CORS(app, resources={r"/*": {"origins": "*"}})

# API/OpenAPI configuration
app.config["API_TITLE"] = "Complaint Data Analysis API"
app.config["API_VERSION"] = "v1"
app.config["OPENAPI_VERSION"] = "3.0.3"
app.config["OPENAPI_URL_PREFIX"] = "/docs"
app.config["OPENAPI_SWAGGER_UI_PATH"] = ""
app.config["OPENAPI_SWAGGER_UI_URL"] = "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"
# Tag groups
app.config["API_SPEC_OPTIONS"] = {
    "tags": [
        {"name": "Healt Check", "description": "Health check route"},
        {"name": "Complaints", "description": "Upload and analysis of complaint data"},
    ]
}

api = Api(app)
# Register blueprints
api.register_blueprint(health_blp)
api.register_blueprint(blp_complaints)

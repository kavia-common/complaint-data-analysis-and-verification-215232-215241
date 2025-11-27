from flask_smorest import Blueprint
from flask.views import MethodView

blp = Blueprint("Healt Check", "health check", url_prefix="/", description="Health check route")


@blp.route("/")
class HealthCheck(MethodView):
    """
    PUBLIC_INTERFACE
    Health check endpoint.
    Returns 200 with a simple message to indicate the API is running.
    """
    @blp.doc(summary="Health check", description="Returns a static message indicating service health.")
    def get(self):
        return {"message": "Healthy"}

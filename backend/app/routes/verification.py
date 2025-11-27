from flask_smorest import Blueprint
from flask.views import MethodView
from marshmallow import Schema, fields, INCLUDE
from typing import Any, Dict, List
from ..services.verification import verify_complaints

# Blueprint for verification APIs grouped under /api
blp_verification = Blueprint(
    "Verification",
    "verification",
    url_prefix="/api",
    description="Endpoints to verify complaint records using rule checks"
)


class ComplaintRecordSchema(Schema):
    class Meta:
        unknown = INCLUDE
    # Core fields (optional to keep flexible)
    complaint_id = fields.Str(required=False, description="Complaint identifier")
    device_use_at_time_of_event = fields.Raw(required=False, description="Device use at time of event")
    problem_definition_description = fields.Str(required=False, description="Problem Definition: Description")
    investigation_summary = fields.Str(required=False, description="Investigation: Investigation Summary")
    problem_definition_hazardous_situation = fields.Str(required=False, description="Problem Definition: Hazardous Situation")
    hazard_grid = fields.Raw(required=False, description="Hazard Grid values (array or delimited string)")


class VerifyRequestSchema(Schema):
    # Accept single record or array; we normalize internally
    record = fields.Nested(ComplaintRecordSchema, required=False, description="Single complaint record to verify")
    records = fields.List(fields.Nested(ComplaintRecordSchema), required=False, description="Array of complaint records to verify")


class RuleResultSchema(Schema):
    rule_id = fields.Str(required=True)
    status = fields.Str(required=True, description="PASS|FAIL|WARN|REVIEW")
    message = fields.Str(required=True)
    details = fields.Dict(required=False)


class VerifyResultSchema(Schema):
    complaint_id = fields.Str(required=False)
    overall_status = fields.Str(required=True, description="PASS|FAIL|WARN|REVIEW")
    rule_results = fields.List(fields.Nested(RuleResultSchema), required=True)
    extracted_hazards = fields.List(fields.Str(), required=True)
    missing_hazards = fields.List(fields.Str(), required=True)
    unsupported_hazards = fields.List(fields.Str(), required=True)


class VerifyResponseSchema(Schema):
    results = fields.List(fields.Nested(VerifyResultSchema), required=True)


@blp_verification.route("/verify")
class VerifyComplaints(MethodView):
    """
    PUBLIC_INTERFACE
    Verify complaints against specified rules and return per-complaint results.

    Request (application/json):
      - record: single complaint object
      - or records: array of complaint objects

    Response:
      {
        "results": [
          {
            "complaint_id": "<id or index>",
            "overall_status": "PASS|FAIL|WARN|REVIEW",
            "rule_results": [ ... ],
            "extracted_hazards": [...],
            "missing_hazards": [...],
            "unsupported_hazards": [...]
          }
        ]
      }
    """
    @blp_verification.arguments(VerifyRequestSchema)
    @blp_verification.response(200, VerifyResponseSchema)
    @blp_verification.doc(
        summary="Verify complaint records",
        description="Applies consistency, HS phrase pattern, and hazard grid support rules. Returns per-complaint verification results.",
        tags=["Verification"]
    )
    def post(self, json_args: Dict[str, Any]):
        data: List[Dict[str, Any]] = []
        if json_args.get("records"):
            data = json_args["records"]
        elif json_args.get("record"):
            data = [json_args["record"]]
        else:
            data = []
        return verify_complaints(data)

from flask_smorest import Blueprint, abort
from flask.views import MethodView
from marshmallow import Schema, fields, validates_schema, ValidationError, INCLUDE
from werkzeug.datastructures import FileStorage
from webargs.flaskparser import parser
from typing import Dict, Any, List, Tuple
import csv
import io
import uuid
from datetime import datetime

# Blueprint for complaints APIs grouped under /api
blp_complaints = Blueprint(
    "Complaints",
    "complaints",
    url_prefix="/api/complaints",
    description="Endpoints to upload and analyze complaint CSV data"
)

# In-memory ephemeral stores
UPLOAD_STORE: Dict[str, Dict[str, Any]] = {}
ANALYSIS_STORE: Dict[str, Dict[str, Any]] = {}

REQUIRED_COLUMNS = ["complaint_id", "date", "description", "severity"]


# Schemas

class UploadSummarySchema(Schema):
    row_count = fields.Int(required=True, description="Number of data rows parsed (excluding header).")
    detected_columns = fields.List(fields.Str(), required=True, description="Columns detected in the uploaded CSV.")
    upload_id = fields.Str(required=True, description="Identifier for referencing this uploaded dataset.")


class FileUploadSchema(Schema):
    # For flask-smorest, handle files via type: FileStorage
    file = fields.Raw(required=True, metadata={"type": "file", "format": "binary"}, description="CSV file to upload.")


class AnalyzeRequestSchema(Schema):
    class Meta:
        unknown = INCLUDE
    # Either send a new file or reference an existing upload_id
    file = fields.Raw(required=False, metadata={"type": "file", "format": "binary"}, description="CSV file to analyze.")
    upload_id = fields.Str(required=False, description="Previously returned upload_id to analyze again.")

    @validates_schema
    def validate_either(self, data, **kwargs):
        if not data.get("file") and not data.get("upload_id"):
            raise ValidationError("Either 'file' or 'upload_id' must be provided.")
        if data.get("file") and data.get("upload_id"):
            raise ValidationError("Provide only one of 'file' or 'upload_id', not both.")


class IssueSchema(Schema):
    row_index = fields.Int(required=True, description="Zero-based row index in the data (excluding header).")
    message = fields.Str(required=True, description="Description of the issue for the row.")


class AnalyzeResponseSchema(Schema):
    analysis_id = fields.Str(required=True, description="Identifier to retrieve the analysis report later.")
    completeness = fields.Dict(
        keys=fields.Str(), values=fields.Float(),
        required=True,
        description="Completeness metrics as percentages (0-100)."
    )
    issues = fields.List(fields.Nested(IssueSchema), required=True, description="List of correctness issues found.")
    row_count = fields.Int(required=True, description="Number of data rows evaluated.")
    columns = fields.List(fields.Str(), required=True, description="Columns present in the analyzed dataset.")


class ReportResponseSchema(Schema):
    analysis_id = fields.Str(required=True)
    results = fields.Nested(AnalyzeResponseSchema, required=True)


# Helpers

def _read_csv_file_to_rows(file_storage: FileStorage) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Read a CSV FileStorage into a list of dict rows and columns.
    Assumes UTF-8; uses Python's csv module.
    """
    try:
        # Read bytes and decode for csv
        raw = file_storage.read()
        # Reset stream pointer for any re-reads
        file_storage.stream.seek(0)
        text = raw.decode("utf-8")
    except Exception as e:
        raise ValidationError(f"Unable to read CSV file: {e}")

    f = io.StringIO(text)
    reader = csv.DictReader(f)
    columns = reader.fieldnames or []
    rows = [row for row in reader]
    return rows, columns


def _validate_required_columns(columns: List[str]) -> List[str]:
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    return missing


def _parse_date_safe(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except Exception:
        return False


def _analyze_rows(rows: List[Dict[str, Any]], columns: List[str]) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []

    # Completeness metrics initialization
    total = len(rows)
    if total == 0:
        completeness = {
            "required_columns_present": 100.0 if not _validate_required_columns(columns) else 0.0,
            "date_parseable": 0.0,
            "complaint_id_unique": 0.0,
            "description_non_empty": 0.0,
            "overall_valid_rows": 0.0
        }
        return completeness, issues

    missing_required = _validate_required_columns(columns)
    required_columns_present = 100.0 if not missing_required else 0.0
    if missing_required:
        issues.append({"row_index": -1, "message": f"Missing required columns: {', '.join(missing_required)}"})

    # Row-wise checks
    valid_date_count = 0
    non_empty_desc_count = 0
    valid_rows_count = 0

    seen_ids = set()
    unique_ok_count = 0
    complaint_id_duplicate_rows = []

    # First pass to gather id frequencies
    id_counts: Dict[str, int] = {}
    for r in rows:
        cid = (r.get("complaint_id") or "").strip()
        if cid:
            id_counts[cid] = id_counts.get(cid, 0) + 1

    for idx, r in enumerate(rows):
        row_valid = True

        # date parseable
        date_str = (r.get("date") or "").strip()
        if date_str and _parse_date_safe(date_str):
            valid_date_count += 1
        else:
            issues.append({"row_index": idx, "message": "Invalid or missing date; expected format YYYY-MM-DD"})
            row_valid = False

        # complaint_id uniqueness
        cid = (r.get("complaint_id") or "").strip()
        if cid and id_counts.get(cid, 0) == 1:
            unique_ok_count += 1
        else:
            if not cid:
                issues.append({"row_index": idx, "message": "Missing complaint_id"})
                row_valid = False
            elif id_counts.get(cid, 0) > 1:
                if cid not in seen_ids:
                    # Only flag each duplicate id once at first occurrence
                    issues.append({"row_index": idx, "message": f"Duplicate complaint_id '{cid}'"})
                    complaint_id_duplicate_rows.append(idx)
                row_valid = False
        seen_ids.add(cid)

        # non-empty description
        desc = (r.get("description") or "").strip()
        if desc:
            non_empty_desc_count += 1
        else:
            issues.append({"row_index": idx, "message": "Empty description"})
            row_valid = False

        if row_valid:
            valid_rows_count += 1

    completeness = {
        "required_columns_present": required_columns_present,
        "date_parseable": round((valid_date_count / total) * 100.0, 2),
        "complaint_id_unique": round((unique_ok_count / total) * 100.0, 2),
        "description_non_empty": round((non_empty_desc_count / total) * 100.0, 2),
        "overall_valid_rows": round((valid_rows_count / total) * 100.0, 2),
    }
    return completeness, issues


# Routes

@blp_complaints.route("/upload")
class ComplaintsUpload(MethodView):
    """
    PUBLIC_INTERFACE
    Upload a complaint CSV file and get a quick summary.

    Request (multipart/form-data):
      - file: CSV file with header row. Required columns: complaint_id, date, description, severity.

    Response:
      - row_count
      - detected_columns
      - upload_id
    """
    @blp_complaints.arguments(FileUploadSchema, location="files")
    @blp_complaints.response(200, UploadSummarySchema)
    @blp_complaints.doc(
        summary="Upload complaint CSV",
        description="Accepts a CSV file and returns a quick summary and an upload_id to reference later."
    )
    def post(self, files_args):
        file_obj = files_args.get("file")
        if not isinstance(file_obj, FileStorage):
            abort(400, message="Invalid 'file' provided.")

        rows, columns = _read_csv_file_to_rows(file_obj)
        upload_id = str(uuid.uuid4())
        UPLOAD_STORE[upload_id] = {
            "rows": rows,
            "columns": columns
        }
        return {
            "row_count": len(rows),
            "detected_columns": columns,
            "upload_id": upload_id
        }


@blp_complaints.route("/analyze")
class ComplaintsAnalyze(MethodView):
    """
    PUBLIC_INTERFACE
    Analyze a complaint CSV for completeness and correctness.

    Request:
      - multipart/form-data with either:
        - file: CSV file, or
        - upload_id: ID returned from previous upload
    Response:
      - analysis_id
      - completeness metrics (%)
      - issues list
      - row_count
      - columns
    """
    @blp_complaints.arguments(AnalyzeRequestSchema, location="form")
    @blp_complaints.response(200, AnalyzeResponseSchema)
    @blp_complaints.doc(
        summary="Analyze complaint data",
        description="Runs checks: required columns present, date parseable (YYYY-MM-DD), unique complaint_id, non-empty description."
    )
    def post(self, form_args):
        rows: List[Dict[str, Any]] = []
        columns: List[str] = []

        file_obj = parser.parse({"file": fields.Raw()}, location="files")
        file_obj = file_obj.get("file", None)

        if file_obj:
            if not isinstance(file_obj, FileStorage):
                abort(400, message="Invalid 'file' provided.")
            rows, columns = _read_csv_file_to_rows(file_obj)
        else:
            upload_id = (form_args.get("upload_id") or "").strip()
            if not upload_id:
                abort(400, message="Missing 'upload_id' when no file is provided.")
            stored = UPLOAD_STORE.get(upload_id)
            if not stored:
                abort(404, message="upload_id not found. Upload a file first.")
            rows = stored["rows"]
            columns = stored["columns"]

        completeness, issues = _analyze_rows(rows, columns)
        analysis_id = str(uuid.uuid4())
        result = {
            "analysis_id": analysis_id,
            "completeness": completeness,
            "issues": issues,
            "row_count": len(rows),
            "columns": columns
        }
        ANALYSIS_STORE[analysis_id] = result
        return result


@blp_complaints.route("/report/<string:analysis_id>")
class ComplaintsReport(MethodView):
    """
    PUBLIC_INTERFACE
    Retrieve a previously computed analysis report by ID.

    Path:
      - analysis_id: Identifier returned by /api/complaints/analyze
    Response:
      - analysis_id
      - results: AnalyzeResponseSchema
    """
    @blp_complaints.response(200, ReportResponseSchema)
    @blp_complaints.doc(
        summary="Get analysis report",
        description="Returns details for a previously computed analysis by analysis_id."
    )
    def get(self, analysis_id: str):
        results = ANALYSIS_STORE.get(analysis_id)
        if not results:
            abort(404, message="analysis_id not found.")
        return {"analysis_id": analysis_id, "results": results}

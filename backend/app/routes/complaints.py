from flask_smorest import Blueprint, abort
from flask.views import MethodView
from marshmallow import Schema, fields, validates_schema, ValidationError, INCLUDE
from werkzeug.datastructures import FileStorage
from webargs.flaskparser import parser
from typing import Dict, Any, List, Tuple, Set
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

# Core required columns for baseline checks
REQUIRED_COLUMNS = ["complaint_id", "date", "description", "severity"]

# Optional columns for hazardous situation validation rules
HS_OPTIONAL_COLUMNS = [
    "device_use_at_time_of_event",
    "problem_definition_description",
    "investigation_summary",
    "problem_definition_hazardous_situation",
    "hazard_grid"  # can be a delimited list of hazards e.g., "electrical; thermal"
]

# Rule codes per requirements
RULE_CODES = {
    "HS_NO_HS_WHEN_HAZARDS": "HS_NO_HS_WHEN_HAZARDS",
    "HS_DEVICE_USE_MISMATCH": "HS_DEVICE_USE_MISMATCH",
    "HS_UNKNOWN_DEVICE_USE_REVIEW": "HS_UNKNOWN_DEVICE_USE_REVIEW",
    "HS_HAZARD_GRID_MISSING": "HS_HAZARD_GRID_MISSING",
    "HS_HAZARD_GRID_MISMATCH": "HS_HAZARD_GRID_MISMATCH",
}

# Simple keyword sets to derive hazards from free text (stdlib only)
HAZARD_KEYWORDS: Dict[str, Set[str]] = {
    "electrical": {"shock", "electrocute", "electrical", "short circuit", "spark", "power surge"},
    "thermal": {"burn", "overheat", "hot", "thermal", "scald"},
    "mechanical": {"pinch", "crush", "breakage", "fracture", "mechanical", "shear"},
    "biological": {"infection", "contamination", "bio", "bacterial", "viral", "fungal"},
    "chemical": {"corrosive", "toxic", "chemical", "solvent", "acid", "alkali"},
    "radiation": {"radiation", "x-ray", "radioactive", "gamma", "uv"},
    "software": {"bug", "software", "crash", "firmware", "hang", "error code"},
    "use error": {"misuse", "user error", "use error", "incorrect use", "misinterpret"},
}

# Normalization helpers for device use
DEVICE_USE_NORMALIZATION = {
    "in use": "in_use",
    "in-use": "in_use",
    "in_use": "in_use",
    "being used": "in_use",
    "not in use": "not_in_use",
    "not-in-use": "not_in_use",
    "stored": "not_in_use",
    "transport": "not_in_use",
    "unknown": "unknown",
    "n/a": "unknown",
    "na": "unknown",
    "": "unknown",
    None: "unknown"
}

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
    code = fields.Str(required=False, description="Rule/violation code.")
    details = fields.Dict(required=False, description="Structured details for the violation.")
    fields = fields.List(fields.Str(), required=False, description="CSV column names involved in the violation.")


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
    # Additional summary metrics for new HS rules
    hs_summary = fields.Dict(
        keys=fields.Str(), values=fields.Int(),
        required=False,
        description="Summary counts for hazardous situation validation rules."
    )
    # Derived hazards aggregated across dataset
    derived_hazards = fields.List(
        fields.Str(),
        required=False,
        description="Unique set of hazards derived from narrative across all rows."
    )
    # Summary counters for quick UI badges
    summary = fields.Dict(
        keys=fields.Str(), values=fields.Int(),
        required=False,
        description="High-level counters: total_violations, rows_with_violations, rows_without_violations."
    )


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
        raw = file_storage.read()
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


def _norm(s: Any) -> str:
    """Normalize general free-text values for comparison."""
    if s is None:
        return ""
    return str(s).strip().lower()


def _split_hazard_grid(value: str) -> List[str]:
    """
    Parse hazard_grid column into a normalized list of hazards.
    Supports separators ; , | and handles extra whitespace.
    """
    if not value:
        return []
    raw = _norm(value)
    parts = []
    for sep in [";", ",", "|"]:
        if sep in raw:
            parts = [p.strip() for p in raw.split(sep)]
            break
    if not parts:
        parts = [raw]
    # remove empties and normalize
    return [p for p in (p.strip() for p in parts) if p]


def _normalize_device_use(value: Any) -> str:
    """Normalize device use categories into in_use, not_in_use, or unknown."""
    key = _norm(value)
    return DEVICE_USE_NORMALIZATION.get(key, DEVICE_USE_NORMALIZATION.get(key.replace(" ", "_"), "unknown"))


def _derive_hazards_from_text(*texts: str) -> Set[str]:
    """
    Derive a set of hazards by simple keyword matching across provided texts.
    Uses HAZARD_KEYWORDS; returns normalized hazard names that matched.
    """
    joined = " ".join(_norm(t) for t in texts if t)
    derived: Set[str] = set()
    for hz, keywords in HAZARD_KEYWORDS.items():
        for kw in keywords:
            if kw in joined:
                derived.add(hz)
                break
    return derived


def _row_hs_checks(row: Dict[str, Any], colmap: Dict[str, str]) -> List[Dict[str, Any]]:
    """
    Perform hazardous situation related checks on a single row.
    Returns a list of issue dicts with code, message, details.
    """
    issues: List[Dict[str, Any]] = []

    # Extract and normalize relevant fields
    device_use = _normalize_device_use(row.get(colmap.get("device_use_at_time_of_event", ""), ""))
    desc = row.get(colmap.get("problem_definition_description", ""), "") or row.get("description", "")
    inv_sum = row.get(colmap.get("investigation_summary", ""), "")
    hs_text = row.get(colmap.get("problem_definition_hazardous_situation", ""), "")
    hz_grid_raw = row.get(colmap.get("hazard_grid", ""), "")

    # Normalize hazardous situation text value
    hs_norm = _norm(hs_text)
    no_hs_values = {"no hazardous situation", "no hs", "none", "n/a", "na", ""}
    has_no_hs = hs_norm in no_hs_values

    # Parse hazard grid list
    hazard_grid_list = _split_hazard_grid(hz_grid_raw)
    hazard_grid_set = set(hazard_grid_list)

    # Derive hazards from description + investigation summary
    derived_hazards = _derive_hazards_from_text(desc, inv_sum)

    # Rule: Grid missing
    if colmap.get("hazard_grid") and not hazard_grid_list and (desc or inv_sum):
        issues.append({
            "code": RULE_CODES["HS_HAZARD_GRID_MISSING"],
            "message": "Hazard Grid is empty but narrative content exists.",
            "details": {
                "device_use": device_use,
                "derived_hazards": sorted(list(derived_hazards)),
                "hazard_grid": []
            },
            "fields": [c for c in [colmap.get("hazard_grid") or "hazard_grid",
                                   colmap.get("problem_definition_description") or "description",
                                   colmap.get("investigation_summary") or "investigation_summary"] if c]
        })

    # Rule: No Hazardous Situation cannot coexist with hazards in grid
    if has_no_hs and hazard_grid_list:
        issues.append({
            "code": RULE_CODES["HS_NO_HS_WHEN_HAZARDS"],
            "message": "Hazardous Situation marked as 'No Hazardous Situation' but hazards are present in Hazard Grid.",
            "details": {
                "hazardous_situation": hs_norm,
                "hazard_grid": hazard_grid_list
            },
            "fields": [c for c in [colmap.get("problem_definition_hazardous_situation") or "problem_definition_hazardous_situation",
                                   colmap.get("hazard_grid") or "hazard_grid"] if c]
        })

    # Rule: Mismatch between derived hazards and hazard grid
    if derived_hazards:
        # We consider names normalized like in HAZARD_KEYWORDS keys
        # Map grid strings to normalized keys if they match known hazard labels
        normalized_grid = { _norm(h) for h in hazard_grid_set }
        # if derived not subset of grid -> mismatch
        if not derived_hazards.issubset(normalized_grid):
            issues.append({
                "code": RULE_CODES["HS_HAZARD_GRID_MISMATCH"],
                "message": "Derived hazards from narrative do not match Hazard Grid.",
                "details": {
                    "derived_hazards": sorted(list(derived_hazards)),
                    "hazard_grid": sorted(list(normalized_grid))
                },
                "fields": [c for c in [colmap.get("hazard_grid") or "hazard_grid",
                                       colmap.get("problem_definition_description") or "description",
                                       colmap.get("investigation_summary") or "investigation_summary"] if c]
            })

    # Rule: Device use mismatch with HS
    # If device was "not_in_use" but hazards found in grid or narrative indicate active use hazards (e.g., electrical/thermal/mechanical),
    # then likely mismatch.
    active_use_hazards = {"electrical", "thermal", "mechanical", "radiation"}
    narrative_active = bool(derived_hazards & active_use_hazards)
    grid_active = bool({ _norm(h) for h in hazard_grid_set } & active_use_hazards)

    if device_use == "not_in_use" and (narrative_active or grid_active):
        issues.append({
            "code": RULE_CODES["HS_DEVICE_USE_MISMATCH"],
            "message": "Device reported as 'not in use' while hazards indicate active device interaction.",
            "details": {
                "device_use": device_use,
                "derived_hazards": sorted(list(derived_hazards)),
                "hazard_grid": sorted(list(hazard_grid_set))
            },
            "fields": [c for c in [colmap.get("device_use_at_time_of_event") or "device_use_at_time_of_event",
                                   colmap.get("hazard_grid") or "hazard_grid",
                                   colmap.get("problem_definition_description") or "description",
                                   colmap.get("investigation_summary") or "investigation_summary"] if c]
        })

    # Rule: Unknown device use should be flagged for review when hazards exist
    if device_use == "unknown" and (derived_hazards or hazard_grid_list):
        issues.append({
            "code": RULE_CODES["HS_UNKNOWN_DEVICE_USE_REVIEW"],
            "message": "Device use at time of event is unknown; review recommended due to hazards present.",
            "details": {
                "device_use": device_use,
                "derived_hazards": sorted(list(derived_hazards)),
                "hazard_grid": sorted(list(hazard_grid_set))
            },
            "fields": [c for c in [colmap.get("device_use_at_time_of_event") or "device_use_at_time_of_event",
                                   colmap.get("hazard_grid") or "hazard_grid",
                                   colmap.get("problem_definition_description") or "description",
                                   colmap.get("investigation_summary") or "investigation_summary"] if c]
        })

    return issues


def _map_optional_columns(columns: List[str]) -> Dict[str, str]:
    """
    Build a map from canonical optional keys to actual CSV column names present.
    Allows flexible matching via case-insensitive and simplified keys.
    """
    lc_cols = {c.lower(): c for c in columns}
    mapping: Dict[str, str] = {}

    def find_col(candidates: List[str]) -> str:
        for cand in candidates:
            if cand in lc_cols:
                return lc_cols[cand]
        return ""

    mapping["device_use_at_time_of_event"] = find_col([
        "device use at time of event",
        "device_use_at_time_of_event",
        "device use",
        "device_use",
        "use at time of event",
    ])
    mapping["problem_definition_description"] = find_col([
        "problem definition: description",
        "problem_definition_description",
        "description",
    ])
    mapping["investigation_summary"] = find_col([
        "investigation: investigation summary",
        "investigation_summary",
        "investigation summary",
    ])
    mapping["problem_definition_hazardous_situation"] = find_col([
        "problem definition: hazardous situation",
        "problem_definition_hazardous_situation",
        "hazardous situation",
        "hazardous_situation",
        "hs",
    ])
    mapping["hazard_grid"] = find_col([
        "hazard grid",
        "hazard_grid",
        "hazards",
    ])

    return mapping


def _analyze_rows(rows: List[Dict[str, Any]], columns: List[str]) -> Tuple[Dict[str, float], List[Dict[str, Any]], Dict[str, int], List[str], Dict[str, int]]:
    """
    Perform baseline completeness checks and hazardous situation/device use validation rules.

    Returns:
      - completeness metrics
      - issues list (with per-row codes/details)
      - hs_summary: counts of hazardous situation rule violations by code
    """
    issues: List[Dict[str, Any]] = []
    hs_summary: Dict[str, int] = {code: 0 for code in RULE_CODES.values()}
    dataset_derived_hazards: Set[str] = set()

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
        # No rows; nothing to derive
        summary = {
            "total_violations": 0,
            "rows_with_violations": 0,
            "rows_without_violations": 0
        }
        return completeness, issues, hs_summary, sorted(list(dataset_derived_hazards)), summary, [], summary

    missing_required = _validate_required_columns(columns)
    required_columns_present = 100.0 if not missing_required else 0.0
    if missing_required:
        issues.append({"row_index": -1, "message": f"Missing required columns: {', '.join(missing_required)}", "fields": missing_required})

    # Row-wise checks
    valid_date_count = 0
    non_empty_desc_count = 0
    valid_rows_count = 0

    seen_ids = set()
    unique_ok_count = 0

    # First pass to gather id frequencies
    id_counts: Dict[str, int] = {}
    for r in rows:
        cid = (r.get("complaint_id") or "").strip()
        if cid:
            id_counts[cid] = id_counts.get(cid, 0) + 1

    # Optional column mapping for HS rules
    colmap = _map_optional_columns(columns)

    rows_with_violation: Set[int] = set()
    for idx, r in enumerate(rows):
        row_valid = True

        # date parseable
        date_str = (r.get("date") or "").strip()
        if date_str and _parse_date_safe(date_str):
            valid_date_count += 1
        else:
            issues.append({"row_index": idx, "message": "Invalid or missing date; expected format YYYY-MM-DD", "fields": ["date"]})
            row_valid = False
            rows_with_violation.add(idx)

        # complaint_id uniqueness
        cid = (r.get("complaint_id") or "").strip()
        if cid and id_counts.get(cid, 0) == 1:
            unique_ok_count += 1
        else:
            if not cid:
                issues.append({"row_index": idx, "message": "Missing complaint_id", "fields": ["complaint_id"]})
                row_valid = False
                rows_with_violation.add(idx)
            elif id_counts.get(cid, 0) > 1:
                if cid not in seen_ids:
                    issues.append({"row_index": idx, "message": f"Duplicate complaint_id '{cid}'", "fields": ["complaint_id"]})
                row_valid = False
                rows_with_violation.add(idx)
        seen_ids.add(cid)

        # non-empty description (prefer specific column if present)
        desc_col = colmap.get("problem_definition_description") or "description"
        desc = (r.get(desc_col) or "").strip()
        if desc:
            non_empty_desc_count += 1
        else:
            issues.append({"row_index": idx, "message": "Empty description", "fields": [desc_col]})
            row_valid = False
            rows_with_violation.add(idx)

        # Hazardous situation / device use validations
        hs_issues = _row_hs_checks(r, colmap)
        for iss in hs_issues:
            iss["row_index"] = idx
            issues.append(iss)
            rows_with_violation.add(idx)
            code = iss.get("code")
            if code:
                hs_summary[code] = hs_summary.get(code, 0) + 1
        # Aggregate derived hazards from this row's narrative to dataset-level set
        # Re-derive using same logic to avoid leaking internals out of helper
        from_text = _derive_hazards_from_text((r.get(colmap.get("problem_definition_description") or "description") or ""),
                                              (r.get(colmap.get("investigation_summary") or "") or ""))
        dataset_derived_hazards.update(from_text)

        if row_valid:
            valid_rows_count += 1

    completeness = {
        "required_columns_present": required_columns_present,
        "date_parseable": round((valid_date_count / total) * 100.0, 2),
        "complaint_id_unique": round((unique_ok_count / total) * 100.0, 2),
        "description_non_empty": round((non_empty_desc_count / total) * 100.0, 2),
        "overall_valid_rows": round((valid_rows_count / total) * 100.0, 2),
    }
    return completeness, issues, hs_summary


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
      - issues list (with codes/details for HS rules)
      - row_count
      - columns
      - hs_summary: counts per HS rule
    """
    @blp_complaints.arguments(AnalyzeRequestSchema, location="form")
    @blp_complaints.response(200, AnalyzeResponseSchema)
    @blp_complaints.doc(
        summary="Analyze complaint data",
        description=(
            "Runs checks: required columns present, date parseable (YYYY-MM-DD), unique complaint_id, "
            "non-empty description. Adds HS rules: checks consistency among Device Use, Hazardous Situation text, "
            "Hazard Grid, and derived hazards from narrative. Returns per-row violations with codes: "
            "HS_NO_HS_WHEN_HAZARDS, HS_DEVICE_USE_MISMATCH, HS_UNKNOWN_DEVICE_USE_REVIEW, "
            "HS_HAZARD_GRID_MISSING, HS_HAZARD_GRID_MISMATCH."
        )
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

        completeness, issues, hs_summary, derived_hazards, summary = _analyze_rows(rows, columns)
        analysis_id = str(uuid.uuid4())
        result = {
            "analysis_id": analysis_id,
            "completeness": completeness,
            "issues": issues,
            "row_count": len(rows),
            "columns": columns,
            "hs_summary": hs_summary,
            "derived_hazards": derived_hazards,
            "summary": summary
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

"""
Verification service for complaint data.

Implements rules:
1) Consistency between Device Use, Problem Definition Description, Investigation Summary, and Hazardous Situation text.
2) If Hazard Grid has one or more hazards, Hazardous Situation must NOT contain "No Hazardous Situation".
3) Device use must match acceptable Hazardous Situation phrase patterns.
4) Verify correct Hazard(s) in Hazard Grid based on Description and Investigation Summary.

PUBLIC_INTERFACE functions are documented below.
"""
import re
from typing import Any, Dict, List, Tuple, Set


# Acceptable device use normalized values
ACCEPTABLE_DEVICE_USE = {"in_use", "not_in_use", "unknown"}

# Regex patterns for Hazardous Situation text based on device use with [HAZARD] placeholder
# We compile regexes that capture the hazard token
# Outside of use
PATTERNS_OUTSIDE_USE = [
    re.compile(r"patient\s+exposure\s+to\s+(.+?)\s+outside\s+clinical\s+use", re.IGNORECASE),
    re.compile(r"user\s+exposure\s+to\s+(.+?)\s+outside\s+clinical\s+use", re.IGNORECASE),
    re.compile(r"environment\s+exposure\s+to\s+(.+?)", re.IGNORECASE),
    re.compile(r"property\s+exposure\s+to\s+(.+?)", re.IGNORECASE),
]
# In use
PATTERNS_IN_USE = [
    re.compile(r"patient\s+exposure\s+to\s+(.+?)\s+during\s+clinical\s+use", re.IGNORECASE),
    re.compile(r"consumer\s+exposure\s+to\s+(.+?)\s+during\s+clinical\s+use", re.IGNORECASE),
    re.compile(r"user\s+exposure\s+to\s+(.+?)\s+during\s+clinical\s+use", re.IGNORECASE),
    re.compile(r"environment\s+exposure\s+to\s+(.+?)", re.IGNORECASE),
    re.compile(r"property\s+exposure\s+to\s+(.+?)", re.IGNORECASE),
]

# Simple hazard keyword seeds (can be extended). Used as hints when scanning narrative.
SEED_HAZARD_KEYWORDS: Dict[str, Set[str]] = {
    "electrical": {"shock", "electrocute", "electrical", "short circuit", "spark", "power surge"},
    "thermal": {"burn", "overheat", "hot", "thermal", "scald"},
    "mechanical": {"pinch", "crush", "breakage", "fracture", "mechanical", "shear"},
    "biological": {"infection", "contamination", "bio", "bacterial", "viral", "fungal"},
    "chemical": {"corrosive", "toxic", "chemical", "solvent", "acid", "alkali"},
    "radiation": {"radiation", "x-ray", "radioactive", "gamma", "uv"},
    "software": {"bug", "software", "crash", "firmware", "hang", "error code"},
    "use error": {"misuse", "user error", "use error", "incorrect use", "misinterpret"},
}


def normalize_text(value: Any) -> str:
    """
    Normalize text to lowercased, stripped string.
    """
    if value is None:
        return ""
    return str(value).strip().lower()


def _safe_get(d: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    """
    Safe lookup for possibly variant keys. Returns first found, else default.
    """
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _normalize_device_use(value: Any) -> str:
    """
    Normalize device use field into one of: in_use, not_in_use, unknown.
    """
    v = normalize_text(value)
    mapping = {
        "in use": "in_use",
        "in-use": "in_use",
        "in_use": "in_use",
        "being used": "in_use",
        "use": "in_use",
        "not in use": "not_in_use",
        "not-in-use": "not_in_use",
        "not_in_use": "not_in_use",
        "outside of use": "not_in_use",
        "stored": "not_in_use",
        "transport": "not_in_use",
        "unknown": "unknown",
        "n/a": "unknown",
        "na": "unknown",
        "": "unknown",
    }
    return mapping.get(v, mapping.get(v.replace("  ", " ").replace("_", " "), "unknown"))


def _tokenize_candidates(text: str) -> List[str]:
    """
    Split text using commas/semicolons/pipes/lines to get candidate hazard phrases.
    """
    if not text:
        return []
    parts = re.split(r"[,\;\|\n]+", text)
    return [p.strip() for p in parts if p and p.strip()]


# PUBLIC_INTERFACE
def extract_hazards_from_text(text: str, known_hazards_hint: List[str] = None) -> List[str]:
    """
    Extract hazard keywords from text using simple heuristics.

    Strategy:
    - Look for explicit "exposure to <hazard>" patterns and capture the hazard token.
    - Split on commas/semicolons and pick tokens that align with known hazard hints.
    - Keyword seeding: if any seed keywords appear, add the canonical hazard name.

    Args:
        text: Input text (description or investigation summary).
        known_hazards_hint: Optional list of hazard strings (e.g., from hazard_grid) to bias extraction.

    Returns:
        List of unique, lowercased hazard terms.
    """
    text_norm = normalize_text(text)
    found: Set[str] = set()

    # Regex pattern to capture "... exposure to <hazard> ..."
    for m in re.finditer(r"exposure\s+to\s+([a-z0-9\-\s/]+)", text_norm, flags=re.IGNORECASE):
        token = m.group(1).strip(" .")
        if token:
            found.add(token)

    # Use seed keywords to infer canonical names
    for hz, kws in SEED_HAZARD_KEYWORDS.items():
        for kw in kws:
            if kw in text_norm:
                found.add(hz)
                break

    # Use hints from grid if present
    hints = set()
    if known_hazards_hint:
        for h in known_hazards_hint:
            h_norm = normalize_text(h)
            if h_norm:
                hints.add(h_norm)

    # Token candidates by splitting punctuation
    for token in _tokenize_candidates(text_norm):
        if token in hints:
            found.add(token)

    return sorted(found)


def _match_patterns(patterns: List[re.Pattern], hs_text: str) -> Tuple[bool, str, str]:
    """
    Try to match any of the provided regex patterns against HS text.
    Returns:
      matched: bool
      matched_pattern_label: normalized description of the matched template
      hazard_token: extracted hazard token (may be empty)
    """
    for pat in patterns:
        m = pat.search(hs_text or "")
        if m:
            hazard = m.group(1).strip() if m.groups() else ""
            # Build a readable label by replacing hazard token with placeholder
            label = pat.pattern
            # Simplify to human-readable based on which list we used
            if "outside" in pat.pattern.lower():
                if "patient" in pat.pattern.lower():
                    label = "Patient Exposure to [HAZARD] Outside Clinical Use"
                elif "user" in pat.pattern.lower():
                    label = "User Exposure to [HAZARD] Outside Clinical Use"
                else:
                    label = "Exposure Outside Clinical Use"
            elif "during" in pat.pattern.lower():
                if "patient" in pat.pattern.lower():
                    label = "Patient Exposure to [HAZARD] During Clinical Use"
                elif "consumer" in pat.pattern.lower():
                    label = "Consumer Exposure to [HAZARD] During Clinical Use"
                elif "user" in pat.pattern.lower():
                    label = "User Exposure to [HAZARD] During Clinical Use"
                else:
                    label = "Exposure During Clinical Use"
            elif "environment" in pat.pattern.lower():
                label = "Environment Exposure to [HAZARD]"
            elif "property" in pat.pattern.lower():
                label = "Property Exposure to [HAZARD]"
            return True, label, hazard
    return False, "", ""


# PUBLIC_INTERFACE
def match_hazardous_situation(device_use: str, hazardous_situation_text: str) -> Dict[str, Any]:
    """
    Validate hazardous situation text against allowed patterns based on device use.

    Args:
        device_use: normalized device use ("in_use", "not_in_use", "unknown")
        hazardous_situation_text: HS narrative text

    Returns:
        Dict with keys:
         - status: PASS | FAIL | REVIEW
         - matched: bool
         - matched_pattern: label of matched pattern if any
         - extracted_hazard: hazard token if captured
         - message: human-readable message
    """
    hs_norm = normalize_text(hazardous_situation_text or "")

    if device_use == "unknown":
        # Evaluate but do not fail solely due to unknown
        matched, label, hz = _match_patterns(PATTERNS_IN_USE + PATTERNS_OUTSIDE_USE, hs_norm)
        return {
            "status": "REVIEW",
            "matched": matched,
            "matched_pattern": label if matched else "",
            "extracted_hazard": hz if matched else "",
            "message": "Device use unknown; review recommended. Pattern evaluation not strict.",
        }

    if device_use == "in_use":
        matched, label, hz = _match_patterns(PATTERNS_IN_USE, hs_norm)
        if matched:
            return {"status": "PASS", "matched": True, "matched_pattern": label, "extracted_hazard": hz, "message": "Hazardous Situation matches in-use patterns."}
        # If environment/property exposures matched from OUTSIDE/neutral list, allow them to pass as they are generic
        matched_neutral, label2, hz2 = _match_patterns([PATTERNS_IN_USE[3], PATTERNS_IN_USE[4]], hs_norm)  # environment/property
        if matched_neutral:
            return {"status": "PASS", "matched": True, "matched_pattern": label2, "extracted_hazard": hz2, "message": "Generic exposure pattern accepted."}
        return {"status": "FAIL", "matched": False, "matched_pattern": "", "extracted_hazard": "", "message": "Hazardous Situation does not match required in-use patterns."}

    if device_use == "not_in_use":
        matched, label, hz = _match_patterns(PATTERNS_OUTSIDE_USE, hs_norm)
        if matched:
            return {"status": "PASS", "matched": True, "matched_pattern": label, "extracted_hazard": hz, "message": "Hazardous Situation matches outside-of-use patterns."}
        # Allow environment/property generic
        matched_neutral, label2, hz2 = _match_patterns([PATTERNS_OUTSIDE_USE[2], PATTERNS_OUTSIDE_USE[3]], hs_norm)
        if matched_neutral:
            return {"status": "PASS", "matched": True, "matched_pattern": label2, "extracted_hazard": hz2, "message": "Generic exposure pattern accepted."}
        return {"status": "FAIL", "matched": False, "matched_pattern": "", "extracted_hazard": "", "message": "Hazardous Situation does not match required outside-of-use patterns."}

    # Fallback
    return {"status": "REVIEW", "matched": False, "matched_pattern": "", "extracted_hazard": "", "message": "Unrecognized device use; review."}


def _build_rule_result(rule_id: str, status: str, message: str, details: Dict[str, Any] = None) -> Dict[str, Any]:
    return {"rule_id": rule_id, "status": status, "message": message, "details": details or {}}


# PUBLIC_INTERFACE
def verify_complaint(complaint: Dict[str, Any]) -> Dict[str, Any]:
    """
    Verify a single complaint record against rule set.

    Input fields (safe lookups):
      - complaint_id
      - device_use_at_time_of_event
      - problem_definition_description
      - investigation_summary
      - problem_definition_hazardous_situation
      - hazard_grid (list[str] or delimited string)

    Returns:
      {
        "complaint_id": str,
        "overall_status": "PASS|FAIL|WARN|REVIEW",
        "rule_results": [ ... ],
        "extracted_hazards": [...],
        "missing_hazards": [...],
        "unsupported_hazards": [...]
      }
    """
    # Extract fields with resilience to variant names
    cid = _safe_get(complaint, "complaint_id", "id", "case_id", default="")
    device_use_raw = _safe_get(complaint, "device_use_at_time_of_event", "device_use", "use_at_time_of_event", default="")
    desc = _safe_get(complaint, "problem_definition_description", "description", default="")
    inv = _safe_get(complaint, "investigation_summary", "investigation: investigation summary", default="")
    hs_text = _safe_get(complaint, "problem_definition_hazardous_situation", "hazardous_situation", "hazardous situation", default="")
    hz_grid_val = _safe_get(complaint, "hazard_grid", "hazards", default=[])

    # Normalize hazard grid to list[str]
    if isinstance(hz_grid_val, str):
        parts = re.split(r"[;\|,]+", hz_grid_val)
        hazard_grid_list = [normalize_text(p) for p in parts if p and p.strip()]
    elif isinstance(hz_grid_val, list):
        hazard_grid_list = [normalize_text(p) for p in hz_grid_val if p]
    else:
        hazard_grid_list = []

    device_use = _normalize_device_use(device_use_raw)
    hs_norm = normalize_text(hs_text)

    rule_results: List[Dict[str, Any]] = []

    # Rule 2: If Hazard Grid has hazards, HS must not contain "No Hazardous Situation"
    if len(hazard_grid_list) > 0 and "no hazardous situation" in hs_norm:
        rule_results.append(_build_rule_result(
            "HS_NO_NO_HAZARD_WHEN_GRID_HAS",
            "FAIL",
            "Hazard Grid lists hazards but HS states 'No Hazardous Situation'.",
            {"hazard_grid": hazard_grid_list, "hazardous_situation": hs_text}
        ))
    else:
        rule_results.append(_build_rule_result(
            "HS_NO_NO_HAZARD_WHEN_GRID_HAS",
            "PASS",
            "HS text consistent with presence/absence of Hazard Grid."
        ))

    # Rule 3: Device use must match HS patterns
    pattern_result = match_hazardous_situation(device_use, hs_text)
    rule_results.append(_build_rule_result(
        "DEVICE_USE_MATCH_HS_PATTERN",
        pattern_result["status"],
        pattern_result["message"],
        {"device_use": device_use, "matched_pattern": pattern_result.get("matched_pattern", ""), "extracted_hazard": pattern_result.get("extracted_hazard", "")}
    ))

    # Rule 1 and 4: Consistency across Description/Investigation and Hazard Grid
    # Extract hazards from desc+inv
    extracted_hazards = set(extract_hazards_from_text(desc, known_hazards_hint=hazard_grid_list))
    extracted_hazards.update(extract_hazards_from_text(inv, known_hazards_hint=hazard_grid_list))

    grid_set = set(hazard_grid_list)

    missing_hazards = sorted([h for h in extracted_hazards if h not in grid_set])
    unsupported_hazards = sorted([h for h in grid_set if h not in extracted_hazards and h not in SEED_HAZARD_KEYWORDS.keys()])  # allow canonical hazard names even if not in text
    # If either mismatch, set WARN
    if missing_hazards or unsupported_hazards:
        rule_results.append(_build_rule_result(
            "HAZARDS_SUPPORTED_BY_DESC_INV",
            "WARN",
            "Hazard Grid and narrative hazards are not fully aligned.",
            {"missing_hazards": missing_hazards, "unsupported_hazards": unsupported_hazards}
        ))
    else:
        rule_results.append(_build_rule_result(
            "HAZARDS_SUPPORTED_BY_DESC_INV",
            "PASS",
            "Hazard Grid is supported by narrative.",
            {"missing_hazards": [], "unsupported_hazards": []}
        ))

    # Rule 1 (consistency check with device use vs narrative & HS text)
    # Simple heuristic: if device_use == not_in_use but description/investigation mentions clinical/procedure,
    # or HS text contains "during clinical use" then flag FAIL
    narrative = f"{normalize_text(desc)} {normalize_text(inv)}"
    hs_contains_during = "during clinical use" in hs_norm
    mentions_use = any(tok in narrative for tok in ["during clinical use", "while using", "in use", "procedure", "operation"])
    if device_use == "not_in_use" and (hs_contains_during or mentions_use):
        rule_results.append(_build_rule_result(
            "CONSISTENCY_DEVICE_USE_WITH_NARRATIVE",
            "FAIL",
            "Device use marked 'not in use' but narrative/HS indicates use.",
            {"device_use": device_use, "hazardous_situation": hs_text}
        ))
    else:
        rule_results.append(_build_rule_result(
            "CONSISTENCY_DEVICE_USE_WITH_NARRATIVE",
            "PASS",
            "Device use consistent with narrative and HS."
        ))

    # Derive overall status
    overall = "PASS"
    statuses = [r["status"] for r in rule_results]
    if "FAIL" in statuses:
        overall = "FAIL"
    elif "WARN" in statuses:
        overall = "WARN"
    elif "REVIEW" in statuses:
        overall = "REVIEW"

    return {
        "complaint_id": cid or "",
        "overall_status": overall,
        "rule_results": rule_results,
        "extracted_hazards": sorted(list(extracted_hazards)),
        "missing_hazards": missing_hazards,
        "unsupported_hazards": unsupported_hazards,
    }


# PUBLIC_INTERFACE
def verify_complaints(data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Verify one or multiple complaint records.

    Args:
        data: list of complaint dicts (or a single dict wrapped upstream)

    Returns:
        {"results": [ verify_complaint(c) for c in data ]}
    """
    results = [verify_complaint(c or {}) for c in data]
    return {"results": results}

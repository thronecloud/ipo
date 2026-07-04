"""
The analysis output contract.

LLM output is untrusted input. The `--json-schema` flag only *asks* the model to
conform; this module *enforces* it at the persistence boundary. Anything off-contract
(missing key, bad enum, non-integer or out-of-range score, wrong type) is rejected —
never silently summed into a composite or written as a None-riddled row.
"""

import jsonschema

from src.personas import ANALYSIS_JSON_SCHEMA

VALID_RECOMMENDATIONS = {"BUY", "HOLD", "AVOID"}
SCORE_MIN, SCORE_MAX = 0, 10


class AnalysisContractError(ValueError):
    """Raised when an LLM analysis result violates ANALYSIS_JSON_SCHEMA or its ranges."""


def validate_analysis_result(result) -> dict:
    """Return `result` unchanged iff it satisfies the contract; else raise
    AnalysisContractError. Schema handles structure/required/enums; explicit checks
    add the numeric range the schema can't express (0-10) and reject booleans."""
    if not isinstance(result, dict):
        raise AnalysisContractError(f"result is {type(result).__name__}, not an object")
    try:
        jsonschema.validate(result, ANALYSIS_JSON_SCHEMA)
    except jsonschema.ValidationError as e:
        raise AnalysisContractError(f"schema violation: {e.message}") from None

    score = result.get("score")
    if isinstance(score, bool) or not isinstance(score, int):
        raise AnalysisContractError(f"score {score!r} is not an integer")
    if not (SCORE_MIN <= score <= SCORE_MAX):
        raise AnalysisContractError(f"score {score} out of range [{SCORE_MIN},{SCORE_MAX}]")

    rec = result.get("recommendation")
    if rec not in VALID_RECOMMENDATIONS:
        raise AnalysisContractError(f"recommendation {rec!r} not in {sorted(VALID_RECOMMENDATIONS)}")
    return result

from flask import Blueprint, jsonify, request

from auth import auth_required
from database import SessionLocal
from services.analysis_service import (
    DEFAULT_HIGH_STOCK_CASES,
    DEFAULT_LOW_STOCK_CASES,
    build_analysis_overview,
)


analysis_bp = Blueprint("analysis", __name__)


def _read_float_arg(name, default_value):
    raw = request.args.get(name, "")
    if raw is None or str(raw).strip() == "":
        return default_value, None
    try:
        value = float(raw)
    except Exception:
        return None, {"error": f"{name} must be a number"}
    if value < 0:
        return None, {"error": f"{name} cannot be negative"}
    return value, None


@analysis_bp.route("/seller/analysis", methods=["GET"])
@analysis_bp.route("/seller/analysis/overview", methods=["GET"])
@auth_required()
def get_analysis_overview():
    low_stock_cases, err = _read_float_arg("low_stock_cases", DEFAULT_LOW_STOCK_CASES)
    if err:
        return err, 400

    high_stock_cases, err = _read_float_arg("high_stock_cases", DEFAULT_HIGH_STOCK_CASES)
    if err:
        return err, 400

    if high_stock_cases <= low_stock_cases:
        return {"error": "high_stock_cases must be greater than low_stock_cases"}, 400

    db = SessionLocal()
    try:
        payload = build_analysis_overview(
            db,
            low_stock_cases=low_stock_cases,
            high_stock_cases=high_stock_cases,
        )
        return jsonify(payload)
    finally:
        db.close()

from flask import Blueprint, request, jsonify
from sqlalchemy import func
from database import SessionLocal
from models import Invoice, InvoiceTotals, PresentStockDetail, SellReport, SellFinance, PriceListItem
from services.audit import update_last_login, log_action
from auth import authenticate_user, create_token

auth_bp = Blueprint("auth", __name__)

@auth_bp.route("/auth/login", methods=["POST"])
def login():
    payload = request.get_json(silent=True) or {}
    username = payload.get("username", "")
    password = payload.get("password", "")

    user = authenticate_user(username, password)
    if not user:
        return {"error": "Invalid username or password"}, 401

    token = create_token(user["username"], user["role"])
    summary = {}
    db = SessionLocal()
    try:
        update_last_login(db, user)
        log_action(db, user, "login", entity_type="auth", entity_id=user.get("username"))

        last_finance = db.query(SellFinance).order_by(SellFinance.created_at.desc()).first()
        summary["last_uncleared_amount"] = float(last_finance.final_balance or 0.0) if last_finance else 0.0

        last_invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
        summary["last_invoice_date"] = last_invoice.invoice_date if last_invoice else ""
        summary["last_invoice_number"] = last_invoice.invoice_number if last_invoice else ""

        last_invoice_value = 0.0
        last_retailer_credit = 0.0
        if last_invoice:
            totals = db.query(InvoiceTotals).filter(
                InvoiceTotals.invoice_number == last_invoice.invoice_number
            ).first()
            if totals:
                last_invoice_value = float(totals.net_invoice_value or 0.0)
                last_retailer_credit = float(totals.retailer_credit_balance or 0.0)
        summary["last_invoice_value"] = last_invoice_value
        summary["last_invoice_retailer_credit_balance"] = last_retailer_credit

        total_present_stock = db.query(func.coalesce(func.sum(PresentStockDetail.total_cases), 0)).scalar()
        summary["total_present_stock"] = int(total_present_stock or 0)

        mrp_map = {}
        for r in db.query(PriceListItem).all():
            key = (str(r.brand_number or "").strip(), str(r.pack_type or "").strip(), int(r.volume_ml or 0))
            if key not in mrp_map:
                mrp_map[key] = float(r.mrp or 0.0)

        total_present_stock_mrp = 0.0
        stocks = db.query(PresentStockDetail).all()
        for s in stocks:
            key = (str(s.brand_number or "").strip(), str(s.pack_type or "").strip(), int(s.pack_size_quantity_ml or 0))
            mrp = mrp_map.get(key)
            if mrp is None:
                continue
            total_bottles = int(s.total_bottles or 0)
            total_present_stock_mrp += float(mrp) * total_bottles
        summary["total_present_stock_mrp_value"] = total_present_stock_mrp

        last_report = db.query(SellReport).order_by(SellReport.created_at.desc()).first()
        last_report_date = last_report.report_date if last_report else ""
        summary["last_sell_report_date"] = last_report_date
        sell_report_value = 0.0
        if last_report_date:
            sell_report_value = db.query(func.coalesce(func.sum(SellReport.sell_amount), 0.0)).filter(
                SellReport.report_date == last_report_date
            ).scalar()
        summary["last_sell_report_value"] = float(sell_report_value or 0.0)
    finally:
        db.commit()
        db.close()

    return jsonify({
        "access_token": token,
        "token_type": "bearer",
        "role": user["role"],
        "username": user["username"],
        "summary": summary
    })

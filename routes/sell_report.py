import json
import os
from datetime import datetime

from flask import Blueprint, jsonify, request

from auth import auth_required
from database import SessionLocal
from models import Invoice, PresentStockDetail, SellReport
from services.audit import log_action
from services.sales_utils import (
    build_finance_payload,
    build_mrp_map,
    compute_opening_and_additions,
    get_last_finance_balance,
    get_last_reports_by_stock,
    get_previous_report,
    parse_report_date,
    total_bottles,
)
from services.stock_service import recalc_stock_summary

sell_report_bp = Blueprint("sell_report", __name__)


@sell_report_bp.route("/seller/sell-report/prepare", methods=["GET"])
@auth_required()
def prepare_sell_report():
    db = SessionLocal()
    try:
        stocks = db.query(PresentStockDetail).all()
        last_reports = get_last_reports_by_stock(db)
        mrp_map = build_mrp_map(db)
        latest_invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
        latest_invoice_date = latest_invoice.invoice_date if latest_invoice else ""
        latest_sell_report = db.query(SellReport).order_by(SellReport.created_at.desc()).first()
        last_sell_report_date = latest_sell_report.report_date if latest_sell_report else ""
        last_balance_amount = get_last_finance_balance(db)
        payload = []

        for stock in stocks:
            last_report = last_reports.get(stock.id)
            mrp_key = (str(stock.brand_number or "").strip(), int(stock.pack_size_quantity_ml or 0))
            mrp = mrp_map.get(mrp_key)
            opening_cases, opening_bottles, added_cases, added_bottles, total_cases, total_bottles_value = (
                compute_opening_and_additions(db, stock, last_report)
            )

            payload.append({
                "stock_id": stock.id,
                "brand_number": stock.brand_number,
                "brand_name": stock.brand_name,
                "product_type": stock.product_type,
                "pack_size_case": stock.pack_size_case,
                "pack_size_quantity_ml": stock.pack_size_quantity_ml,
                "opening_cases": opening_cases,
                "opening_bottles": opening_bottles,
                "invoice_added_cases": added_cases,
                "invoice_added_bottles": added_bottles,
                "total_cases": total_cases,
                "total_bottles": total_bottles_value,
                "mrp": mrp,
                "last_report_date": last_report.report_date if last_report else "",
                "last_report_at": last_report.created_at.isoformat() if last_report and last_report.created_at else ""
            })

        return jsonify({
            "items": payload,
            "latest_invoice_date": latest_invoice_date,
            "last_sell_report_date": last_sell_report_date,
            "last_balance_amount": last_balance_amount
        })
    finally:
        db.close()


@sell_report_bp.route("/seller/sell-report", methods=["POST"])
@auth_required(roles=["supervisor"])
def create_sell_report():
    payload = request.get_json(silent=True) or {}
    items = payload.get("items", [])
    report_date = payload.get("report_date")
    if not report_date:
        return {"error": "report_date is required"}, 400

    if not isinstance(items, list):
        return {"error": "items must be a list"}, 400
    if not items:
        return jsonify({"status": "ok", "report_date": report_date, "items": []})

    db = SessionLocal()
    try:
        latest_invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
        if not latest_invoice:
            return {"error": "no invoices found"}, 400

        latest_invoice_date = latest_invoice.invoice_date
        report_dt = parse_report_date(report_date)
        invoice_dt = parse_report_date(latest_invoice_date)
        if not report_dt or not invoice_dt:
            return {"error": "invalid report_date or invoice_date format"}, 400
        if report_dt < invoice_dt:
            return {"error": "report_date must be on or after last invoice date"}, 400

        existing_today = db.query(SellReport).filter(SellReport.report_date == report_date).first()
        if existing_today:
            return {"error": "Sell report already created for this date"}, 409

        last_reports = get_last_reports_by_stock(db)
        mrp_map = build_mrp_map(db)
        created = []

        for item in items:
            stock_id = item.get("stock_id")
            closing_cases = item.get("closing_cases", None)
            closing_bottles = item.get("closing_bottles", 0)

            if stock_id is None:
                return {"error": "stock_id is required"}, 400
            if closing_cases is None or str(closing_cases).strip() == "":
                continue

            try:
                closing_cases = int(closing_cases)
                closing_bottles = int(closing_bottles or 0)
            except Exception:
                return {"error": "closing_cases and closing_bottles must be integers"}, 400

            if closing_cases < 0 or closing_bottles < 0:
                return {"error": "closing values cannot be negative"}, 400

            stock = db.query(PresentStockDetail).filter(PresentStockDetail.id == stock_id).first()
            if not stock:
                return {"error": f"stock item not found: {stock_id}"}, 404

            last_report = last_reports.get(stock.id)
            opening_cases, opening_bottles, added_cases, added_bottles, total_cases, total_bottles_value = (
                compute_opening_and_additions(db, stock, last_report)
            )

            closing_total_bottles = total_bottles(closing_cases, closing_bottles, stock.pack_size_case)
            sold_bottles_total = total_bottles_value - closing_total_bottles
            if sold_bottles_total < 0:
                return {
                    "error": f"closing stock exceeds total stock for stock_id {stock_id}",
                    "debug": {
                        "stock_id": stock_id,
                        "opening_cases": opening_cases,
                        "opening_bottles": opening_bottles,
                        "invoice_added_cases": added_cases,
                        "invoice_added_bottles": added_bottles,
                        "total_cases": total_cases,
                        "total_bottles": total_bottles_value,
                        "closing_cases": closing_cases,
                        "closing_bottles": closing_bottles,
                        "pack_size_case": stock.pack_size_case
                    }
                }, 400

            pack_size = int(stock.pack_size_case or 0)
            if pack_size > 0:
                sold_cases = sold_bottles_total // pack_size
                sold_bottles = sold_bottles_total % pack_size
            else:
                sold_cases = 0
                sold_bottles = sold_bottles_total

            unit_rate = stock.unit_rate_per_bottle
            if unit_rate is None and stock.rate_per_case and pack_size > 0:
                unit_rate = float(stock.rate_per_case) / float(pack_size)

            sell_amount = (float(unit_rate) * float(sold_bottles_total)) if unit_rate is not None else None
            mrp_key = (str(stock.brand_number or "").strip(), int(stock.pack_size_quantity_ml or 0))
            mrp = mrp_map.get(mrp_key)

            report = SellReport(
                stock_id=stock.id,
                brand_number=stock.brand_number,
                brand_name=stock.brand_name,
                pack_size_case=stock.pack_size_case,
                pack_size_quantity_ml=stock.pack_size_quantity_ml,
                opening_cases=opening_cases,
                opening_bottles=opening_bottles,
                invoice_added_cases=added_cases,
                invoice_added_bottles=added_bottles,
                total_cases=total_cases,
                total_bottles=total_bottles_value,
                closing_cases=closing_cases,
                closing_bottles=closing_bottles,
                sold_cases=sold_cases,
                sold_bottles=sold_bottles,
                unit_rate_per_bottle=unit_rate,
                sell_amount=sell_amount,
                report_date=report_date,
                created_by=request.user.get("username")
            )
            db.add(report)

            stock.total_cases = closing_cases
            stock.total_bottles = closing_total_bottles
            if unit_rate is not None:
                stock.total_amount = float(closing_total_bottles) * float(unit_rate)
            elif stock.rate_per_case is not None:
                stock.total_amount = float(closing_cases) * float(stock.rate_per_case)

            item_name = stock.brand_name or ""
            item_ml = stock.pack_size_quantity_ml or 0
            stock.last_updated_item_name = f"{item_name} {item_ml}ml/{stock.pack_size_case or 0}"

            created.append({
                "stock_id": stock.id,
                "sold_cases": sold_cases,
                "sold_bottles": sold_bottles,
                "sell_amount": sell_amount,
                "mrp": mrp
            })

        log_action(db, request.user, "create_sell_report", "sell_report", report_date)
        recalc_stock_summary(db)
        db.commit()
        finance_payload = build_finance_payload(db, report_date)
        os.makedirs("output", exist_ok=True)
        safe_date = str(report_date).replace("/", "-").replace("\\", "-")
        out_path = os.path.join("output", f"sell_report_{safe_date}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({
                "report_date": report_date,
                "items": created,
                "finance": finance_payload
            }, f, indent=2)
        return jsonify({
            "status": "ok",
            "report_date": report_date,
            "items": created,
            "finance": finance_payload
        })
    finally:
        db.close()


@sell_report_bp.route("/seller/sell-report/edit-last", methods=["POST"])
@auth_required(roles=["owner"])
def edit_last_sell_report():
    payload = request.get_json(silent=True) or {}
    items = payload.get("items", [])

    if not isinstance(items, list):
        return {"error": "items must be a list"}, 400
    if not items:
        return {"error": "items list is required"}, 400

    db = SessionLocal()
    try:
        last_report = db.query(SellReport).order_by(SellReport.created_at.desc()).first()
        if not last_report:
            return {"error": "no sell report found"}, 404
        already_edited = db.query(SellReport).filter(
            SellReport.report_date == last_report.report_date,
            SellReport.edit_count > 0
        ).first()
        if already_edited:
            return {"error": "sell report already edited once"}, 409

        updated = []
        for item in items:
            stock_id = item.get("stock_id")
            closing_cases = item.get("closing_cases", None)
            closing_bottles = item.get("closing_bottles", 0)

            if stock_id is None:
                return {"error": "stock_id is required"}, 400
            if closing_cases is None or str(closing_cases).strip() == "":
                continue

            try:
                closing_cases = int(closing_cases)
                closing_bottles = int(closing_bottles or 0)
            except Exception:
                return {"error": "closing_cases and closing_bottles must be integers"}, 400

            if closing_cases < 0 or closing_bottles < 0:
                return {"error": "closing values cannot be negative"}, 400

            report = db.query(SellReport).filter(
                SellReport.stock_id == stock_id,
                SellReport.report_date == last_report.report_date
            ).first()
            if not report:
                return {"error": f"sell report item not found: {stock_id}"}, 404

            stock = db.query(PresentStockDetail).filter(PresentStockDetail.id == stock_id).first()
            if not stock:
                return {"error": f"stock item not found: {stock_id}"}, 404

            prev_report = get_previous_report(db, stock_id, report.created_at)
            opening_cases, opening_bottles, added_cases, added_bottles, total_cases, total_bottles_value = (
                compute_opening_and_additions(db, stock, prev_report)
            )

            closing_total_bottles = total_bottles(closing_cases, closing_bottles, stock.pack_size_case)
            sold_bottles_total = total_bottles_value - closing_total_bottles
            if sold_bottles_total < 0:
                return {
                    "error": f"closing stock exceeds total stock for stock_id {stock_id}",
                    "debug": {
                        "stock_id": stock_id,
                        "opening_cases": opening_cases,
                        "opening_bottles": opening_bottles,
                        "invoice_added_cases": added_cases,
                        "invoice_added_bottles": added_bottles,
                        "total_cases": total_cases,
                        "total_bottles": total_bottles_value,
                        "closing_cases": closing_cases,
                        "closing_bottles": closing_bottles,
                        "pack_size_case": stock.pack_size_case
                    }
                }, 400

            pack_size = int(stock.pack_size_case or 0)
            if pack_size > 0:
                sold_cases = sold_bottles_total // pack_size
                sold_bottles = sold_bottles_total % pack_size
            else:
                sold_cases = 0
                sold_bottles = sold_bottles_total

            unit_rate = stock.unit_rate_per_bottle
            if unit_rate is None and stock.rate_per_case and pack_size > 0:
                unit_rate = float(stock.rate_per_case) / float(pack_size)

            sell_amount = (float(unit_rate) * float(sold_bottles_total)) if unit_rate is not None else None

            report.opening_cases = opening_cases
            report.opening_bottles = opening_bottles
            report.invoice_added_cases = added_cases
            report.invoice_added_bottles = added_bottles
            report.total_cases = total_cases
            report.total_bottles = total_bottles_value
            report.closing_cases = closing_cases
            report.closing_bottles = closing_bottles
            report.sold_cases = sold_cases
            report.sold_bottles = sold_bottles
            report.unit_rate_per_bottle = unit_rate
            report.sell_amount = sell_amount
            report.edited_by = request.user.get("username")
            report.edited_at = datetime.utcnow()
            report.edit_count = 1

            stock.total_cases = closing_cases
            stock.total_bottles = closing_total_bottles
            if unit_rate is not None:
                stock.total_amount = float(closing_total_bottles) * float(unit_rate)
            elif stock.rate_per_case is not None:
                stock.total_amount = float(closing_cases) * float(stock.rate_per_case)

            item_name = stock.brand_name or ""
            item_ml = stock.pack_size_quantity_ml or 0
            stock.last_updated_item_name = f"{item_name} {item_ml}ml/{stock.pack_size_case or 0}"

            updated.append({
                "stock_id": stock.id,
                "sold_cases": sold_cases,
                "sold_bottles": sold_bottles,
                "sell_amount": sell_amount
            })

        log_action(db, request.user, "edit_sell_report", "sell_report", last_report.report_date)
        recalc_stock_summary(db)
        db.commit()
        return jsonify({"status": "ok", "report_date": last_report.report_date, "items": updated})
    finally:
        db.close()

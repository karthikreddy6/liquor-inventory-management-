from flask import Blueprint, request, jsonify
from sqlalchemy import func
from sqlalchemy.exc import OperationalError
import time
from datetime import datetime
import json
import os

from database import SessionLocal
from models import PresentStockDetail, Invoice, InvoiceItem, SellReport, PriceListItem, SellFinance, SellFinanceExpense
from services.stock_service import recalc_stock_summary
from auth import jwt_required
from services.audit import log_action

sales_bp = Blueprint("sales", __name__)


def _get_last_reports_by_stock(db):
    last_reports = {}
    try:
        rows = db.query(SellReport).order_by(SellReport.created_at.desc()).all()
        for r in rows:
            if r.stock_id not in last_reports:
                last_reports[r.stock_id] = r
    except OperationalError:
        return {}
    return last_reports


def _get_previous_report(db, stock_id, before_dt):
    if before_dt is None:
        return None
    return db.query(SellReport).filter(
        SellReport.stock_id == stock_id,
        SellReport.created_at < before_dt
    ).order_by(SellReport.created_at.desc()).first()


def _invoice_additions(db, stock, since_dt):
    q = db.query(
        func.coalesce(func.sum(InvoiceItem.cases_delivered), 0),
        func.coalesce(func.sum(InvoiceItem.bottles_delivered), 0)
    ).join(Invoice, Invoice.invoice_number == InvoiceItem.invoice_number)

    q = q.filter(
        InvoiceItem.brand_number == stock.brand_number,
        InvoiceItem.pack_size_case == stock.pack_size_case,
        InvoiceItem.pack_size_quantity_ml == stock.pack_size_quantity_ml,
    )

    if since_dt is not None:
        q = q.filter(Invoice.created_at > since_dt)

    cases, bottles = q.first()
    return int(cases or 0), int(bottles or 0)


def _total_bottles(cases, loose_bottles, pack_size_case):
    return int(cases or 0) * int(pack_size_case or 0) + int(loose_bottles or 0)

def _split_bottles(total_bottles, pack_size_case):
    pack = int(pack_size_case or 0)
    total = int(total_bottles or 0)
    if pack <= 0:
        return 0, total
    cases = total // pack
    bottles = total % pack
    return cases, bottles

def _compute_opening_and_additions(db, stock, last_report):
    if last_report:
        opening_cases = last_report.closing_cases
        opening_bottles = last_report.closing_bottles
        since_dt = last_report.created_at
        added_cases, added_bottles = _invoice_additions(db, stock, since_dt)
    else:
        opening_cases = 0
        opening_bottles = 0
        added_cases, added_bottles = _invoice_additions(db, stock, None)

    total_cases = int(opening_cases or 0) + int(added_cases or 0)
    opening_total_bottles = _total_bottles(opening_cases, opening_bottles, stock.pack_size_case)
    added_total_bottles = _total_bottles(added_cases, added_bottles, stock.pack_size_case)
    total_bottles = opening_total_bottles + added_total_bottles

    return opening_cases, opening_bottles, added_cases, added_bottles, total_cases, total_bottles


def _build_mrp_map(db):
    mrp_map = {}
    try:
        rows = db.query(PriceListItem).all()
    except OperationalError:
        return {}
    for r in rows:
        key = (str(r.brand_number or "").strip(), int(r.volume_ml or 0))
        if key not in mrp_map:
            mrp_map[key] = r.mrp
    return mrp_map


def _get_total_sell_amount(db, report_date):
    total = db.query(func.coalesce(func.sum(SellReport.sell_amount), 0.0)).filter(
        SellReport.report_date == report_date
    ).scalar()
    try:
        return float(total or 0.0)
    except Exception:
        return 0.0


def _get_last_finance_balance(db, exclude_finance_id=None):
    q = db.query(SellFinance)
    if exclude_finance_id is not None:
        q = q.filter(SellFinance.id != exclude_finance_id)
    last_finance = q.order_by(SellFinance.created_at.desc()).first()
    if not last_finance:
        return 0.0
    try:
        return float(last_finance.final_balance or 0.0)
    except Exception:
        return 0.0


def _parse_report_date(val):
    if not val:
        return None
    val = str(val).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(val, fmt).date()
        except Exception:
            continue
    return None


@sales_bp.route("/seller/sell-report/prepare", methods=["GET"])
@jwt_required(roles=["owner", "supervisor"])
def prepare_sell_report():
    db = SessionLocal()
    try:
        stocks = db.query(PresentStockDetail).all()
        last_reports = _get_last_reports_by_stock(db)
        mrp_map = _build_mrp_map(db)
        latest_invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
        latest_invoice_date = latest_invoice.invoice_date if latest_invoice else ""
        last_balance_amount = _get_last_finance_balance(db)
        payload = []

        for stock in stocks:
            last_report = last_reports.get(stock.id)
            mrp_key = (str(stock.brand_number or "").strip(), int(stock.pack_size_quantity_ml or 0))
            mrp = mrp_map.get(mrp_key)
            opening_cases, opening_bottles, added_cases, added_bottles, total_cases, total_bottles = (
                _compute_opening_and_additions(db, stock, last_report)
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
                "total_bottles": total_bottles,
                "mrp": mrp,
                "last_report_date": last_report.report_date if last_report else "",
                "last_report_at": last_report.created_at.isoformat() if last_report and last_report.created_at else ""
            })

        return jsonify({
            "items": payload,
            "latest_invoice_date": latest_invoice_date,
            "last_balance_amount": last_balance_amount
        })
    finally:
        db.close()


@sales_bp.route("/seller/sell-report", methods=["POST"])
@jwt_required(roles=["supervisor"])
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
        report_dt = _parse_report_date(report_date)
        invoice_dt = _parse_report_date(latest_invoice_date)
        if not report_dt or not invoice_dt:
            return {"error": "invalid report_date or invoice_date format"}, 400
        if report_dt < invoice_dt:
            return {"error": "report_date must be on or after last invoice date"}, 400

        existing_today = db.query(SellReport).filter(SellReport.report_date == report_date).first()
        if existing_today:
            return {"error": "Sell report already created for this date"}, 409

        last_reports = _get_last_reports_by_stock(db)
        mrp_map = _build_mrp_map(db)
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
            opening_cases, opening_bottles, added_cases, added_bottles, total_cases, total_bottles = (
                _compute_opening_and_additions(db, stock, last_report)
            )

            closing_total_bottles = _total_bottles(closing_cases, closing_bottles, stock.pack_size_case)
            sold_bottles_total = total_bottles - closing_total_bottles
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
                        "total_bottles": total_bottles,
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
                total_bottles=total_bottles,
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
        os.makedirs("output", exist_ok=True)
        safe_date = str(report_date).replace("/", "-").replace("\\", "-")
        out_path = os.path.join("output", f"sell_report_{safe_date}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"report_date": report_date, "items": created}, f, indent=2)
        return jsonify({"status": "ok", "report_date": report_date, "items": created})
    finally:
        db.close()


@sales_bp.route("/seller/sell-report/edit-last", methods=["POST"])
@jwt_required(roles=["owner"])
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

            prev_report = _get_previous_report(db, stock_id, report.created_at)
            opening_cases, opening_bottles, added_cases, added_bottles, total_cases, total_bottles = (
                _compute_opening_and_additions(db, stock, prev_report)
            )

            closing_total_bottles = _total_bottles(closing_cases, closing_bottles, stock.pack_size_case)
            sold_bottles_total = total_bottles - closing_total_bottles
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
                        "total_bottles": total_bottles,
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
            report.total_bottles = total_bottles
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


@sales_bp.route("/seller/sell-finance", methods=["POST"])
@jwt_required(roles=["owner", "supervisor"])
def create_sell_finance():
    payload = request.get_json(silent=True) or {}
    report_date = payload.get("report_date")
    upi_phonepay = payload.get("upi_phonepay", 0)
    cash = payload.get("cash", 0)
    expenses = payload.get("expenses", [])

    if not report_date:
        return {"error": "report_date is required"}, 400
    if not isinstance(expenses, list):
        return {"error": "expenses must be a list"}, 400

    db = SessionLocal()
    try:
        sell_report_exists = db.query(SellReport).filter(
            SellReport.report_date == report_date
        ).first()
        if not sell_report_exists:
            return {"error": "sell report not found for this date"}, 404

        finance = db.query(SellFinance).filter(
            SellFinance.report_date == report_date
        ).first()

        last_balance_amount = _get_last_finance_balance(db, finance.id if finance else None)
        total_sell_amount = _get_total_sell_amount(db, report_date)

        try:
            upi_phonepay = float(upi_phonepay or 0.0)
            cash = float(cash or 0.0)
        except Exception:
            return {"error": "upi_phonepay and cash must be numbers"}, 400

        total_amount = float(total_sell_amount) + float(last_balance_amount)
        total_balance = float(upi_phonepay) + float(cash) - float(total_amount)

        total_expenses = 0.0
        cleaned_expenses = []
        for exp in expenses:
            name = str(exp.get("name", "")).strip()
            amount = exp.get("amount", 0)
            if not name:
                continue
            try:
                amount = float(amount or 0.0)
            except Exception:
                return {"error": "expense amount must be a number"}, 400
            total_expenses += amount
            cleaned_expenses.append({"name": name, "amount": amount})

        final_balance = float(total_balance) - float(total_expenses)

        if finance:
            finance.total_sell_amount = total_sell_amount
            finance.last_balance_amount = last_balance_amount
            finance.total_amount = total_amount
            finance.upi_phonepay = upi_phonepay
            finance.cash = cash
            finance.total_balance = total_balance
            finance.total_expenses = total_expenses
            finance.final_balance = final_balance
            finance.updated_by = request.user.get("username")

            db.query(SellFinanceExpense).filter(
                SellFinanceExpense.finance_id == finance.id
            ).delete()
        else:
            finance = SellFinance(
                report_date=report_date,
                total_sell_amount=total_sell_amount,
                last_balance_amount=last_balance_amount,
                total_amount=total_amount,
                upi_phonepay=upi_phonepay,
                cash=cash,
                total_balance=total_balance,
                total_expenses=total_expenses,
                final_balance=final_balance,
                created_by=request.user.get("username"),
                updated_by=request.user.get("username")
            )
            db.add(finance)
            db.flush()

        for exp in cleaned_expenses:
            db.add(SellFinanceExpense(
                finance_id=finance.id,
                name=exp["name"],
                amount=exp["amount"]
            ))

        log_action(db, request.user, "create_sell_finance", "sell_finance", report_date)
        db.commit()
        return jsonify({
            "status": "ok",
            "report_date": report_date,
            "total_sell_amount": total_sell_amount,
            "last_balance_amount": last_balance_amount,
            "total_amount": total_amount,
            "upi_phonepay": upi_phonepay,
            "cash": cash,
            "total_balance": total_balance,
            "total_expenses": total_expenses,
            "final_balance": final_balance,
            "expenses": cleaned_expenses
        })
    finally:
        db.close()


@sales_bp.route("/seller/sell-finance/prepare", methods=["GET"])
@jwt_required(roles=["owner", "supervisor"])
def prepare_sell_finance():
    report_date = request.args.get("report_date")
    if not report_date:
        return {"error": "report_date is required"}, 400

    db = SessionLocal()
    try:
        sell_report_exists = db.query(SellReport).filter(
            SellReport.report_date == report_date
        ).first()
        if not sell_report_exists:
            return {"error": "sell report not found for this date"}, 404

        finance = db.query(SellFinance).filter(
            SellFinance.report_date == report_date
        ).first()

        last_balance_amount = _get_last_finance_balance(db, finance.id if finance else None)
        total_sell_amount = _get_total_sell_amount(db, report_date)
        total_amount = float(total_sell_amount) + float(last_balance_amount)

        return jsonify({
            "report_date": report_date,
            "total_sell_amount": total_sell_amount,
            "last_balance_amount": last_balance_amount,
            "total_amount": total_amount,
            "existing_finance": bool(finance)
        })
    finally:
        db.close()

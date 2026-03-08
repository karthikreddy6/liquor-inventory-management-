import json
import os
from datetime import datetime

from flask import Blueprint, jsonify, request

from auth import auth_required
from database import SessionLocal
from models import (
    Invoice,
    PresentStockDetail,
    PriceListItem,
    SellReport,
    UserBrandAlias,
    UserBrandSortPreference,
)
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


def _normalize_brand_number(value):
    return str(value or "").strip()


def _stock_alpha_key(stock):
    return (
        str(stock.brand_name or "").strip().lower(),
        _normalize_brand_number(stock.brand_number),
        int(stock.pack_size_quantity_ml or 0),
        int(stock.id or 0),
    )


def _get_user_brand_sort_order(db, username):
    if not username:
        return []
    rows = db.query(UserBrandSortPreference).filter(
        UserBrandSortPreference.username == username
    ).order_by(UserBrandSortPreference.sort_index.asc()).all()
    return [_normalize_brand_number(r.brand_number) for r in rows if _normalize_brand_number(r.brand_number)]


def _get_user_brand_alias_map(db, username):
    if not username:
        return {}
    rows = db.query(UserBrandAlias).filter(
        UserBrandAlias.username == username
    ).all()
    alias_map = {}
    for r in rows:
        brand_number = _normalize_brand_number(r.brand_number)
        short_name = str(r.short_name or "").strip()
        if brand_number and short_name:
            alias_map[brand_number] = short_name
    return alias_map


def _build_price_list_brand_catalog(db, alias_map=None):
    alias_map = alias_map or {}
    rows = db.query(PriceListItem).order_by(PriceListItem.product_name.asc()).all()
    catalog = []
    seen = set()
    for r in rows:
        brand_number = _normalize_brand_number(r.brand_number)
        if not brand_number or brand_number in seen:
            continue
        seen.add(brand_number)
        brand_name = str(r.product_name or "").strip() or brand_number
        catalog.append({
            "brand_number": brand_number,
            "brand_name": brand_name,
            "display_brand_name": alias_map.get(brand_number, brand_name),
        })
    return catalog


def _build_brand_name_map(brand_catalog):
    return {b["brand_number"]: b["brand_name"] for b in brand_catalog}


def _build_custom_list_preview(user_brand_order, brand_name_map, alias_map=None):
    alias_map = alias_map or {}
    preview = []
    for idx, brand_number in enumerate(user_brand_order, start=1):
        brand_name = brand_name_map.get(brand_number, brand_number)
        preview.append({
            "position": idx,
            "brand_number": brand_number,
            "brand_name": brand_name,
            "display_brand_name": alias_map.get(brand_number, brand_name),
        })
    return preview


def _sort_stocks(stocks, sort_mode, user_brand_order):
    mode = str(sort_mode or "alpha").strip().lower()
    if mode == "brand_number":
        return sorted(
            stocks,
            key=lambda s: (
                _normalize_brand_number(s.brand_number),
                str(s.brand_name or "").strip().lower(),
                int(s.pack_size_quantity_ml or 0),
                int(s.id or 0),
            ),
        )

    if mode == "custom" and user_brand_order:
        order_index = {bn: idx for idx, bn in enumerate(user_brand_order)}
        return sorted(
            stocks,
            key=lambda s: (
                0 if _normalize_brand_number(s.brand_number) in order_index else 1,
                order_index.get(_normalize_brand_number(s.brand_number), 10**9),
                *_stock_alpha_key(s),
            ),
        )

    return sorted(stocks, key=_stock_alpha_key)


@sell_report_bp.route("/seller/sell-report/prepare", methods=["GET"])
@auth_required()
def prepare_sell_report():
    db = SessionLocal()
    try:
        sort_mode = request.args.get("sort_mode", "alpha")
        username = (request.user or {}).get("username")
        user_brand_order = _get_user_brand_sort_order(db, username)
        user_alias_map = _get_user_brand_alias_map(db, username)
        stocks = db.query(PresentStockDetail).all()
        stocks = _sort_stocks(stocks, sort_mode, user_brand_order)
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
                "display_brand_name": user_alias_map.get(_normalize_brand_number(stock.brand_number), stock.brand_name),
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
            "last_balance_amount": last_balance_amount,
            "sort_mode": str(sort_mode or "alpha").strip().lower(),
            "custom_brand_order": user_brand_order,
            "brand_aliases": user_alias_map,
        })
    finally:
        db.close()


@sell_report_bp.route("/seller/sell-report/sort-order", methods=["GET"])
@auth_required()
def get_sell_report_sort_order():
    db = SessionLocal()
    try:
        username = (request.user or {}).get("username")
        if not username:
            return {"error": "invalid user"}, 401
        brand_order = _get_user_brand_sort_order(db, username)
        alias_map = _get_user_brand_alias_map(db, username)
        brand_catalog = _build_price_list_brand_catalog(db, alias_map=alias_map)
        brand_name_map = _build_brand_name_map(brand_catalog)
        return jsonify({
            "username": username,
            "brand_order": brand_order,
            "last_custom_list_preview": _build_custom_list_preview(brand_order, brand_name_map, alias_map),
            "brand_aliases": alias_map,
        })
    finally:
        db.close()


@sell_report_bp.route("/seller/sell-report/brands", methods=["GET"])
@auth_required()
def list_sell_report_brands():
    db = SessionLocal()
    try:
        username = (request.user or {}).get("username")
        if not username:
            return {"error": "invalid user"}, 401

        alias_map = _get_user_brand_alias_map(db, username)
        brand_catalog = _build_price_list_brand_catalog(db, alias_map=alias_map)
        brand_name_map = _build_brand_name_map(brand_catalog)
        user_brand_order = _get_user_brand_sort_order(db, username)
        selected = set(user_brand_order)
        return jsonify({
            "brands": brand_catalog,
            "custom_brand_order": user_brand_order,
            "last_custom_list_preview": _build_custom_list_preview(user_brand_order, brand_name_map, alias_map),
            "remaining_brands": [b for b in brand_catalog if b["brand_number"] not in selected],
            "brand_aliases": alias_map,
        })
    finally:
        db.close()


@sell_report_bp.route("/seller/sell-report/sort-order", methods=["POST"])
@auth_required()
def save_sell_report_sort_order():
    payload = request.get_json(silent=True) or {}
    raw_order = payload.get("brand_order", payload.get("brand_numbers", []))
    if raw_order is None:
        raw_order = []
    if not isinstance(raw_order, list):
        return {"error": "brand_order must be a list"}, 400

    db = SessionLocal()
    try:
        username = (request.user or {}).get("username")
        if not username:
            return {"error": "invalid user"}, 401

        seen = set()
        normalized_order = []
        for value in raw_order:
            brand_number = _normalize_brand_number(value)
            if not brand_number or brand_number in seen:
                continue
            seen.add(brand_number)
            normalized_order.append(brand_number)

        existing_brand_numbers = {
            _normalize_brand_number(r[0])
            for r in db.query(PriceListItem.brand_number).distinct().all()
            if _normalize_brand_number(r[0])
        }
        invalid = [bn for bn in normalized_order if bn not in existing_brand_numbers]
        if invalid:
            return {
                "error": "unknown brand_number values found",
                "invalid_brand_numbers": invalid
            }, 400

        db.query(UserBrandSortPreference).filter(
            UserBrandSortPreference.username == username
        ).delete(synchronize_session=False)

        for idx, brand_number in enumerate(normalized_order):
            db.add(UserBrandSortPreference(
                username=username,
                brand_number=brand_number,
                sort_index=idx,
            ))

        db.commit()
        alias_map = _get_user_brand_alias_map(db, username)
        brand_catalog = _build_price_list_brand_catalog(db, alias_map=alias_map)
        brand_name_map = _build_brand_name_map(brand_catalog)
        return jsonify({
            "status": "ok",
            "username": username,
            "brand_order": normalized_order,
            "last_custom_list_preview": _build_custom_list_preview(normalized_order, brand_name_map, alias_map),
            "brand_aliases": alias_map,
        })
    finally:
        db.close()


@sell_report_bp.route("/seller/sell-report/sort-order/add", methods=["POST"])
@auth_required()
def add_brand_to_sell_report_sort_order():
    payload = request.get_json(silent=True) or {}
    brand_number = _normalize_brand_number(payload.get("brand_number"))
    if not brand_number:
        return {"error": "brand_number is required"}, 400

    db = SessionLocal()
    try:
        username = (request.user or {}).get("username")
        if not username:
            return {"error": "invalid user"}, 401

        exists_in_price_list = db.query(PriceListItem).filter(
            PriceListItem.brand_number == brand_number
        ).first()
        if not exists_in_price_list:
            return {"error": "brand_number not found in price list"}, 404

        existing = db.query(UserBrandSortPreference).filter(
            UserBrandSortPreference.username == username,
            UserBrandSortPreference.brand_number == brand_number,
        ).first()
        if existing:
            order = _get_user_brand_sort_order(db, username)
            return jsonify({
                "status": "ok",
                "message": "brand already in custom list",
                "username": username,
                "brand_order": order
            })

        max_index = db.query(UserBrandSortPreference).filter(
            UserBrandSortPreference.username == username
        ).order_by(UserBrandSortPreference.sort_index.desc()).first()
        next_index = (int(max_index.sort_index) + 1) if max_index else 0

        db.add(UserBrandSortPreference(
            username=username,
            brand_number=brand_number,
            sort_index=next_index,
        ))
        db.commit()

        order = _get_user_brand_sort_order(db, username)
        alias_map = _get_user_brand_alias_map(db, username)
        brand_catalog = _build_price_list_brand_catalog(db, alias_map=alias_map)
        brand_name_map = _build_brand_name_map(brand_catalog)
        return jsonify({
            "status": "ok",
            "username": username,
            "brand_order": order,
            "last_custom_list_preview": _build_custom_list_preview(order, brand_name_map, alias_map),
            "brand_aliases": alias_map,
        })
    finally:
        db.close()


@sell_report_bp.route("/seller/sell-report/brand-aliases", methods=["GET"])
@auth_required()
def get_sell_report_brand_aliases():
    db = SessionLocal()
    try:
        username = (request.user or {}).get("username")
        if not username:
            return {"error": "invalid user"}, 401
        alias_map = _get_user_brand_alias_map(db, username)
        return jsonify({"username": username, "brand_aliases": alias_map})
    finally:
        db.close()


@sell_report_bp.route("/seller/sell-report/brand-alias", methods=["POST"])
@auth_required()
def set_sell_report_brand_alias():
    payload = request.get_json(silent=True) or {}
    brand_number = _normalize_brand_number(payload.get("brand_number"))
    short_name = str(payload.get("short_name") or "").strip()

    if not brand_number:
        return {"error": "brand_number is required"}, 400
    if not short_name:
        return {"error": "short_name is required"}, 400
    if len(short_name) > 20:
        return {"error": "short_name max length is 20"}, 400

    db = SessionLocal()
    try:
        username = (request.user or {}).get("username")
        if not username:
            return {"error": "invalid user"}, 401

        exists_in_price_list = db.query(PriceListItem).filter(
            PriceListItem.brand_number == brand_number
        ).first()
        if not exists_in_price_list:
            return {"error": "brand_number not found in price list"}, 404

        row = db.query(UserBrandAlias).filter(
            UserBrandAlias.username == username,
            UserBrandAlias.brand_number == brand_number,
        ).first()
        if row:
            row.short_name = short_name
        else:
            db.add(UserBrandAlias(
                username=username,
                brand_number=brand_number,
                short_name=short_name,
            ))

        db.commit()
        alias_map = _get_user_brand_alias_map(db, username)
        return jsonify({
            "status": "ok",
            "username": username,
            "brand_number": brand_number,
            "short_name": short_name,
            "brand_aliases": alias_map,
        })
    finally:
        db.close()


@sell_report_bp.route("/seller/sell-report/brand-alias/<brand_number>", methods=["DELETE"])
@auth_required()
def delete_sell_report_brand_alias(brand_number):
    db = SessionLocal()
    try:
        username = (request.user or {}).get("username")
        if not username:
            return {"error": "invalid user"}, 401
        brand_number = _normalize_brand_number(brand_number)
        if not brand_number:
            return {"error": "brand_number is required"}, 400

        db.query(UserBrandAlias).filter(
            UserBrandAlias.username == username,
            UserBrandAlias.brand_number == brand_number,
        ).delete(synchronize_session=False)
        db.commit()
        alias_map = _get_user_brand_alias_map(db, username)
        return jsonify({
            "status": "ok",
            "username": username,
            "brand_aliases": alias_map,
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

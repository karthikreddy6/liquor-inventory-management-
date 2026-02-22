from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename
import os
from datetime import datetime
from database import SessionLocal
from models import Invoice, InvoiceItem, InvoiceTotals, PresentStockDetail, StockSummary, PriceListItem
from pdf_parser import parse_invoice_pdf
from config import INVOICES_FOLDER
from services.files import save_invoice_file
from auth import auth_required
from services.audit import log_action

upload_bp = Blueprint("upload", __name__)

@upload_bp.route("/upload/preview", methods=["POST"])
@auth_required()
def upload_preview():
    if "file" not in request.files:
        return {"error": "No file"}, 400

    file = request.files["file"]
    filename = secure_filename(file.filename)
    temp_dir = os.path.join("output", "preview")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, filename)
    file.save(temp_path)

    try:
        data = parse_invoice_pdf(temp_path)
        retailer_code = str(data.get("retailer", {}).get("code", "")).strip()
        if retailer_code != "2500552":
            return {"error": "Retailer code mismatch. Expected 2500552."}, 400
        invoice_number = data.get("invoice_meta", {}).get("invoice_number", "")
        if invoice_number:
            db = SessionLocal()
            try:
                exists = db.query(Invoice).filter(Invoice.invoice_number == invoice_number).first()
                if exists:
                    return {"error": f"Invoice already exists: {invoice_number}"}, 409
            finally:
                db.close()
        return jsonify({"preview": data})
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass

@upload_bp.route("/upload", methods=["POST"])
@auth_required()
def upload_pdf():
    if "file" not in request.files:
        return {"error": "No file"}, 400

    file = request.files["file"]
    filename = secure_filename(file.filename)
    path = os.path.join(INVOICES_FOLDER, filename)
    file.save(path)

    data = parse_invoice_pdf(path)
    retailer_code = str(data.get("retailer", {}).get("code", "")).strip()
    if retailer_code != "2500552":
        if os.path.exists(path):
            os.remove(path)
        return {"error": "Retailer code mismatch. Expected 2500552."}, 400
    save_invoice_file(
        upload_path=path,
        invoice_date=data.get("invoice_meta", {}).get("invoice_date", ""),
        invoice_number=data.get("invoice_meta", {}).get("invoice_number", "")
    )

    db = SessionLocal()
    try:
        invoice = Invoice(
            invoice_number=data["invoice_meta"]["invoice_number"],
            invoice_date=data["invoice_meta"]["invoice_date"],
            retailer_name=data["retailer"]["name"],
            retailer_code=data["retailer"]["code"],
            licensee_pan=data["licensee"]["pan"],
            uploaded_by=request.user.get("username"),
            uploaded_at=datetime.utcnow()
        )
        db.add(invoice)
        db.flush()

        invoice_id = invoice.id
        invoice_number = invoice.invoice_number

        totals_data = data.get("totals", {})
        totals = InvoiceTotals(
            invoice_number=invoice_number,
            e_challan_amount=totals_data.get("e_challan_amount", 0.0),
            previous_credit=totals_data.get("previous_credit", 0.0),
            sub_total=totals_data.get("sub_total", 0.0),
            special_excise_cess=totals_data.get("special_excise_cess", 0.0),
            tcs=totals_data.get("tcs", 0.0),
            less_this_invoice_value=totals_data.get("less_this_invoice_value", 0.0),
            retailer_credit_balance=totals_data.get("retailer_credit_balance", 0.0),
            invoice_value=totals_data.get("invoice_value", 0.0),
            mrp_round_off=totals_data.get("mrp_round_off", 0.0),
            net_invoice_value=totals_data.get("net_invoice_value", 0.0),
            total_invoice_value=totals_data.get("total_invoice_value", 0.0)
        )
        db.add(totals)

        invoice_date = invoice.invoice_date
        summary = db.query(StockSummary).first()
        if not summary:
            summary = StockSummary(
                total_cases_all_items=0,
                total_price_all_items=0.0
            )
            db.add(summary)

        for item in data["items"]:
            db_item = InvoiceItem(invoice_number=invoice_number, **item)
            db.add(db_item)

            item_name = item.get("brand_name") or ""
            item_ml = item.get("pack_size_quantity_ml") or 0
            item_case = item.get("pack_size_case") or 0
            item_display = f"{item_name} {item_ml}ml/{item_case}"

            price_row = db.query(PriceListItem).filter(
                PriceListItem.brand_number == item.get("brand_number"),
                PriceListItem.pack_type == item.get("pack_type"),
                PriceListItem.volume_ml == item.get("pack_size_quantity_ml")
            ).first()
            mrp = float(price_row.mrp) if price_row and price_row.mrp is not None else None
            item_cases = item.get("cases_delivered") or 0
            item_bottles = item.get("bottles_delivered") or 0
            total_bottles = int(item_cases) * int(item_case or 0) + int(item_bottles)
            unit_rate = mrp
            rate_per_case = (float(mrp) * float(item_case)) if (mrp is not None and item_case) else None
            total_amount = (float(mrp) * float(total_bottles)) if mrp is not None else 0.0

            stock = db.query(PresentStockDetail).filter(
                PresentStockDetail.brand_number == item.get("brand_number"),
                PresentStockDetail.pack_size_case == item.get("pack_size_case"),
                PresentStockDetail.pack_size_quantity_ml == item.get("pack_size_quantity_ml")
            ).first()

            if stock:
                stock.total_cases = (stock.total_cases or 0) + (item.get("cases_delivered") or 0)
                stock.total_bottles = (stock.total_bottles or 0) + (item.get("bottles_delivered") or 0)
                stock.total_amount = (stock.total_amount or 0.0) + total_amount
                if unit_rate is not None:
                    stock.unit_rate_per_bottle = unit_rate
                if rate_per_case is not None:
                    stock.rate_per_case = rate_per_case
                stock.last_invoice_date = invoice_date
                stock.last_updated_item_name = item_display
            else:
                stock = PresentStockDetail(
                    brand_number=item.get("brand_number"),
                    brand_name=item.get("brand_name"),
                    product_type=item.get("product_type"),
                    pack_type=item.get("pack_type"),
                    pack_size_case=item.get("pack_size_case"),
                    pack_size_quantity_ml=item.get("pack_size_quantity_ml"),
                    total_cases=item.get("cases_delivered") or 0,
                    total_bottles=item.get("bottles_delivered") or 0,
                    rate_per_case=rate_per_case,
                    unit_rate_per_bottle=unit_rate,
                    total_amount=total_amount,
                    last_invoice_date=invoice_date,
                    last_updated_item_name=item_display
                )
                db.add(stock)

            summary.total_cases_all_items = (summary.total_cases_all_items or 0) + (item.get("cases_delivered") or 0)
            summary.total_price_all_items = (summary.total_price_all_items or 0.0) + total_amount
            summary.last_updated_item_name = item_display

        log_action(db, request.user, "upload_invoice", "invoice", invoice_number)
        db.commit()
    finally:
        db.close()

    return jsonify({
        "invoice_id": invoice_id,
        "invoice": data
    })

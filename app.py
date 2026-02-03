from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
import os
from flask_cors import CORS
from database import SessionLocal
from models import Invoice, InvoiceItem, InvoiceTotals, PresentStockDetail, StockSummary
from pdf_parser import parse_invoice_pdf
from database import SessionLocal, Base, engine
import sys
sys.path.append(".")

app = Flask(__name__)
CORS(app)
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
INVOICES_FOLDER = "invoices"
os.makedirs(INVOICES_FOLDER, exist_ok=True)

def save_invoice_file(upload_path: str, invoice_date: str, invoice_number: str) -> str:
    if not invoice_date:
        invoice_date = "unknown-date"
    base_name = secure_filename(invoice_date)
    if not base_name:
        base_name = "invoice"
    suffix = secure_filename(invoice_number) or "file"
    filename = f"{base_name}-{suffix}.pdf"
    target_path = os.path.join(INVOICES_FOLDER, filename)
    if os.path.exists(target_path):
        counter = 2
        while True:
            filename = f"{base_name}-{suffix}-{counter}.pdf"
            target_path = os.path.join(INVOICES_FOLDER, filename)
            if not os.path.exists(target_path):
                break
            counter += 1
    os.replace(upload_path, target_path)
    return target_path

@app.route("/upload", methods=["POST"])
def upload_pdf():
    if "file" not in request.files:
        return {"error": "No file"}, 400

    file = request.files["file"]
    filename = secure_filename(file.filename)
    path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(path)

    # Parse PDF
    data = parse_invoice_pdf(path)
    save_invoice_file(
        upload_path=path,
        invoice_date=data.get("invoice_meta", {}).get("invoice_date", ""),
        invoice_number=data.get("invoice_meta", {}).get("invoice_number", "")
    )

    # Save to DB
    db = SessionLocal()
    try:
        invoice = Invoice(
            invoice_number=data["invoice_meta"]["invoice_number"],
            invoice_date=data["invoice_meta"]["invoice_date"],
            retailer_name=data["retailer"]["name"],
            retailer_code=data["retailer"]["code"],
            licensee_pan=data["licensee"]["pan"]
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
            net_invoice_value=totals_data.get("net_invoice_value", 0.0)
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

            stock = db.query(PresentStockDetail).filter(
                PresentStockDetail.brand_number == item.get("brand_number"),
                PresentStockDetail.pack_size_case == item.get("pack_size_case"),
                PresentStockDetail.pack_size_quantity_ml == item.get("pack_size_quantity_ml")
            ).first()

            if stock:
                stock.total_cases = (stock.total_cases or 0) + (item.get("cases_delivered") or 0)
                stock.total_bottles = (stock.total_bottles or 0) + (item.get("bottles_delivered") or 0)
                stock.total_amount = (stock.total_amount or 0.0) + (item.get("total_amount") or 0.0)
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
                    rate_per_case=item.get("rate_per_case"),
                    unit_rate_per_bottle=item.get("unit_rate_per_bottle"),
                    total_amount=item.get("total_amount") or 0.0,
                    last_invoice_date=invoice_date,
                    last_updated_item_name=item_display
                )
                db.add(stock)

            summary.total_cases_all_items = (summary.total_cases_all_items or 0) + (item.get("cases_delivered") or 0)
            summary.total_price_all_items = (summary.total_price_all_items or 0.0) + (item.get("total_amount") or 0.0)
            summary.last_updated_item_name = item_display
        
        db.commit()
    finally:
        db.close()
    
    return jsonify({
    "invoice_id": invoice_id,
    "invoice": data
})

@app.route("/stock", methods=["GET"])
def get_stock():
    db = SessionLocal()
    try:
        rows = db.query(PresentStockDetail).all()
        summary = db.query(StockSummary).first()
        stock = []
        for r in rows:
            stock.append({
                "id": r.id,
                "brand_number": r.brand_number,
                "brand_name": r.brand_name,
                "product_type": r.product_type,
                "pack_type": r.pack_type,
                "pack_size_case": r.pack_size_case,
                "pack_size_quantity_ml": r.pack_size_quantity_ml,
                "total_cases": r.total_cases,
                "total_bottles": r.total_bottles,
                "rate_per_case": r.rate_per_case,
                "unit_rate_per_bottle": r.unit_rate_per_bottle,
                "total_amount": r.total_amount,
                "last_invoice_date": r.last_invoice_date,
                "last_updated_item_name": r.last_updated_item_name,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None
            })
        summary_payload = None
        if summary:
            summary_payload = {
                "total_cases_all_items": summary.total_cases_all_items,
                "total_price_all_items": summary.total_price_all_items,
                "last_updated_item_name": summary.last_updated_item_name,
                "updated_at": summary.updated_at.isoformat() if summary.updated_at else None
            }
        return jsonify({"stock": stock, "summary": summary_payload})
    finally:
        db.close()


if __name__ == "__main__":
    app.run(debug=True)

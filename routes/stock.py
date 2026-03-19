from flask import Blueprint, jsonify, send_file
import os
import time
from database import SessionLocal
from models import PresentStockDetail, StockSummary
from auth import auth_required
from services.pdf_export import write_present_stock_pdf

stock_bp = Blueprint("stock", __name__)


def _total_bottles_all_items(rows):
    total = 0
    for row in rows or []:
        pack_size_case = int(row.pack_size_case or 0)
        total_cases = int(row.total_cases or 0)
        total_bottles = int(row.total_bottles or 0)
        total += (total_cases * pack_size_case) + total_bottles
    return total

@stock_bp.route("/stock", methods=["GET"])
@auth_required()
def get_stock():
    db = SessionLocal()
    try:
        rows = db.query(PresentStockDetail).all()
        summary = db.query(StockSummary).first()
        total_bottles_all_items = _total_bottles_all_items(rows)
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
                "total_bottles_all_items": total_bottles_all_items,
                "total_price_all_items": summary.total_price_all_items,
                "last_updated_item_name": summary.last_updated_item_name,
                "updated_at": summary.updated_at.isoformat() if summary.updated_at else None
            }
        return jsonify({"stock": stock, "summary": summary_payload})
    finally:
        db.close()

@stock_bp.route("/stock/pdf", methods=["GET"])
@auth_required()
def download_present_stock_pdf():
    db = SessionLocal()
    try:
        rows = db.query(PresentStockDetail).order_by(PresentStockDetail.brand_name.asc()).all()
        if not rows:
            return {"error": "no stock data"}, 404
        summary = db.query(StockSummary).first()
        total_bottles_all_items = _total_bottles_all_items(rows)

        meta_rows = [
            ["Generated At", time.strftime("%Y-%m-%d %H:%M:%S")],
            ["Total Items", len(rows)],
        ]
        summary_rows = []
        if summary:
            summary_rows = [
                ["Total Bottles (All Items)", total_bottles_all_items],
                ["Total Amount (All Items)", summary.total_price_all_items],
            ]

        items_rows = [[
            "#",
            "Brand No",
            "Brand Name",
            "Pack",
            "Type",
            "Cases",
            "Bottles",
            "Rate/Case",
            "Rate/Bottle",
            "Amount",
            "Last Invoice Date",
        ]]
        for idx, r in enumerate(rows, start=1):
            pack = f"{r.pack_size_case or ''}/{r.pack_size_quantity_ml or ''}ml"
            items_rows.append([
                idx,
                r.brand_number or "",
                r.brand_name or "",
                pack,
                r.pack_type or "",
                r.total_cases or 0,
                r.total_bottles or 0,
                r.rate_per_case if r.rate_per_case is not None else "",
                r.unit_rate_per_bottle if r.unit_rate_per_bottle is not None else "",
                r.total_amount if r.total_amount is not None else "",
                r.last_invoice_date or "",
            ])

        out_dir = os.path.join("requested_pdf", "stock")
        os.makedirs(out_dir, exist_ok=True)
        filename = f"present_stock_{time.strftime('%Y-%m-%d')}.pdf"
        out_path = os.path.join(out_dir, filename)
        write_present_stock_pdf(out_path, meta_rows, items_rows, summary_rows, title="Present Stock Report")
        return send_file(out_path, as_attachment=True, download_name=filename)
    finally:
        db.close()

from flask import Blueprint, jsonify
from database import SessionLocal
from models import PresentStockDetail, StockSummary
from auth import auth_required

stock_bp = Blueprint("stock", __name__)

@stock_bp.route("/stock", methods=["GET"])
@auth_required()
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

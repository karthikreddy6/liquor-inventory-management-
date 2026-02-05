from flask import Blueprint, request, jsonify
from database import SessionLocal
from models import PresentStockDetail
from services.stock_service import recalc_stock_summary
from auth import auth_required

seller_bp = Blueprint("seller", __name__)

@seller_bp.route("/seller/stock-update", methods=["POST"])
@auth_required()
def seller_stock_update():
    payload = request.get_json(silent=True) or {}
    available_cases = payload.get("available_cases", None)
    stock_id = payload.get("stock_id", None)
    brand_number = payload.get("brand_number", None)
    pack_size_case = payload.get("pack_size_case", None)
    pack_size_quantity_ml = payload.get("pack_size_quantity_ml", None)

    if available_cases is None:
        return {"error": "available_cases is required"}, 400

    try:
        available_cases = int(available_cases)
    except Exception:
        return {"error": "available_cases must be an integer"}, 400

    if available_cases < 0:
        return {"error": "available_cases cannot be negative"}, 400

    db = SessionLocal()
    try:
        if stock_id is not None:
            stock = db.query(PresentStockDetail).filter(PresentStockDetail.id == stock_id).first()
        else:
            if not brand_number or pack_size_case is None or pack_size_quantity_ml is None:
                return {
                    "error": "Provide stock_id or brand_number + pack_size_case + pack_size_quantity_ml"
                }, 400
            stock = db.query(PresentStockDetail).filter(
                PresentStockDetail.brand_number == str(brand_number),
                PresentStockDetail.pack_size_case == int(pack_size_case),
                PresentStockDetail.pack_size_quantity_ml == int(pack_size_quantity_ml)
            ).first()

        if not stock:
            return {"error": "stock item not found"}, 404

        bottles_per_case = stock.pack_size_case or 0
        total_bottles = available_cases * bottles_per_case

        stock.total_cases = available_cases
        stock.total_bottles = total_bottles

        if stock.unit_rate_per_bottle is not None:
            stock.total_amount = float(total_bottles) * float(stock.unit_rate_per_bottle)
        elif stock.rate_per_case is not None:
            stock.total_amount = float(available_cases) * float(stock.rate_per_case)

        item_name = stock.brand_name or ""
        item_ml = stock.pack_size_quantity_ml or 0
        stock.last_updated_item_name = f"{item_name} {item_ml}ml/{bottles_per_case}"

        recalc_stock_summary(db)
        db.commit()

        return jsonify({
            "status": "ok",
            "stock_id": stock.id,
            "brand_number": stock.brand_number,
            "brand_name": stock.brand_name,
            "pack_size_case": stock.pack_size_case,
            "pack_size_quantity_ml": stock.pack_size_quantity_ml,
            "total_cases": stock.total_cases,
            "total_bottles": stock.total_bottles,
            "total_amount": stock.total_amount
        })
    finally:
        db.close()

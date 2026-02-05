from models import PresentStockDetail, StockSummary

def recalc_stock_summary(db):
    rows = db.query(PresentStockDetail).all()
    total_cases = 0
    total_amount = 0.0
    last_item_name = None
    for r in rows:
        total_cases += r.total_cases or 0
        total_amount += r.total_amount or 0.0
        last_item_name = r.last_updated_item_name or last_item_name

    summary = db.query(StockSummary).first()
    if not summary:
        summary = StockSummary(
            total_cases_all_items=0,
            total_price_all_items=0.0
        )
        db.add(summary)

    summary.total_cases_all_items = total_cases
    summary.total_price_all_items = total_amount
    summary.last_updated_item_name = last_item_name

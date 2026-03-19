from datetime import datetime

from sqlalchemy import func

from models import (
    Invoice,
    InvoiceTotals,
    PresentStockDetail,
    SellFinance,
    SellReport,
    StockSummary,
)


DEFAULT_LOW_STOCK_CASES = 2.0
DEFAULT_HIGH_STOCK_CASES = 25.0
DEFAULT_MESSAGE_LIMIT = 8
DEFAULT_RECENT_LIMIT = 10
DEFAULT_PREDICTION_REPORTS = 3


def _safe_float(value):
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _safe_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def _stock_total_bottles(row):
    total_bottles = _safe_int(getattr(row, "total_bottles", 0))
    if total_bottles > 0:
        return total_bottles
    pack_size_case = _safe_int(getattr(row, "pack_size_case", 0))
    total_cases = _safe_int(getattr(row, "total_cases", 0))
    return total_cases * pack_size_case


def _stock_equivalent_cases(row):
    pack_size_case = _safe_int(getattr(row, "pack_size_case", 0))
    total_bottles = _stock_total_bottles(row)
    if pack_size_case <= 0:
        return float(total_bottles)
    return float(total_bottles) / float(pack_size_case)


def _report_sold_bottles(report_row):
    pack_size_case = _safe_int(getattr(report_row, "pack_size_case", 0))
    sold_cases = _safe_int(getattr(report_row, "sold_cases", 0))
    sold_bottles = _safe_int(getattr(report_row, "sold_bottles", 0))
    return (sold_cases * pack_size_case) + sold_bottles


def _build_sales_history_map(db):
    rows = db.query(SellReport).order_by(
        SellReport.report_date.desc(),
        SellReport.created_at.desc(),
        SellReport.id.desc(),
    ).all()
    history = {}
    for row in rows:
        history.setdefault(row.stock_id, []).append(row)
    return history


def _predict_required_stock(row, sales_history):
    recent_reports = (sales_history or [])[:DEFAULT_PREDICTION_REPORTS]
    recent_sold_bottles = [_report_sold_bottles(report) for report in recent_reports]
    latest_sold_bottles = recent_sold_bottles[0] if recent_sold_bottles else 0
    avg_recent_sold_bottles = (
        sum(recent_sold_bottles) / len(recent_sold_bottles)
        if recent_sold_bottles else
        0.0
    )
    predicted_required_bottles = 0
    if latest_sold_bottles > 0 or avg_recent_sold_bottles > 0:
        predicted_required_bottles = int(round(max(
            float(latest_sold_bottles),
            float(avg_recent_sold_bottles),
        ) * 1.10))

    pack_size_case = _safe_int(getattr(row, "pack_size_case", 0))
    predicted_required_cases = (
        round(float(predicted_required_bottles) / float(pack_size_case), 2)
        if pack_size_case > 0 else
        float(predicted_required_bottles)
    )
    last_report_date = recent_reports[0].report_date if recent_reports else ""
    return {
        "reports_considered": len(recent_reports),
        "last_report_date": last_report_date,
        "latest_sold_bottles": latest_sold_bottles,
        "average_recent_sold_bottles": round(avg_recent_sold_bottles, 2),
        "predicted_required_bottles": predicted_required_bottles,
        "predicted_required_cases": predicted_required_cases,
    }


def _stock_level(row, prediction, high_cases):
    equivalent_cases = _stock_equivalent_cases(row)
    current_bottles = _stock_total_bottles(row)
    predicted_required_bottles = _safe_int(prediction.get("predicted_required_bottles"))
    if current_bottles <= 0:
        return "zero"
    if predicted_required_bottles > 0 and current_bottles < predicted_required_bottles:
        return "low"
    high_bottles_floor = int(round(float(high_cases) * max(_safe_int(getattr(row, "pack_size_case", 0)), 1)))
    if predicted_required_bottles > 0:
        if current_bottles >= max(predicted_required_bottles * 3, high_bottles_floor):
            return "high"
    elif equivalent_cases >= float(high_cases):
        return "high"
    return "normal"


def _stock_row_payload(row, prediction, high_cases):
    equivalent_cases = _stock_equivalent_cases(row)
    total_bottles = _stock_total_bottles(row)
    predicted_required_bottles = _safe_int(prediction.get("predicted_required_bottles"))
    predicted_required_cases = _safe_float(prediction.get("predicted_required_cases"))
    stock_level = _stock_level(row, prediction, high_cases)
    stock_gap_bottles = total_bottles - predicted_required_bottles
    return {
        "id": row.id,
        "brand_number": row.brand_number,
        "brand_name": row.brand_name,
        "product_type": row.product_type,
        "pack_type": row.pack_type,
        "pack_size_case": row.pack_size_case,
        "pack_size_quantity_ml": row.pack_size_quantity_ml,
        "total_cases": _safe_int(row.total_cases),
        "total_bottles": total_bottles,
        "equivalent_cases": round(equivalent_cases, 2),
        "total_amount": _safe_float(row.total_amount),
        "last_invoice_date": row.last_invoice_date or "",
        "last_updated_item_name": row.last_updated_item_name or "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "reports_considered": _safe_int(prediction.get("reports_considered")),
        "last_sell_report_date": prediction.get("last_report_date", ""),
        "latest_sold_bottles": _safe_int(prediction.get("latest_sold_bottles")),
        "average_recent_sold_bottles": _safe_float(prediction.get("average_recent_sold_bottles")),
        "required_stock_bottles": predicted_required_bottles,
        "required_stock_cases": predicted_required_cases,
        "predicted_required_bottles": predicted_required_bottles,
        "predicted_required_cases": predicted_required_cases,
        "stock_gap_bottles": stock_gap_bottles,
        "shortage_bottles": abs(stock_gap_bottles) if stock_gap_bottles < 0 else 0,
        "excess_bottles": stock_gap_bottles if stock_gap_bottles > 0 else 0,
        "coverage_ratio": round(
            float(total_bottles) / float(predicted_required_bottles), 2
        ) if predicted_required_bottles > 0 else None,
        "stock_level": stock_level,
    }


def _message_from_stock_payload(item):
    qty_ml = _safe_int(item.get("pack_size_quantity_ml"))
    brand = item.get("brand_name") or item.get("brand_number") or "Unknown item"
    required_bottles = _safe_int(item.get("required_stock_bottles"))
    total_bottles = _safe_int(item.get("total_bottles"))
    return f"{brand} {qty_ml}ml: stock {total_bottles}, required {required_bottles}."


def build_analysis_overview(
    db,
    low_stock_cases=DEFAULT_LOW_STOCK_CASES,
    high_stock_cases=DEFAULT_HIGH_STOCK_CASES,
):
    low_stock_cases = _safe_float(low_stock_cases) or DEFAULT_LOW_STOCK_CASES
    high_stock_cases = _safe_float(high_stock_cases) or DEFAULT_HIGH_STOCK_CASES
    if high_stock_cases <= low_stock_cases:
        high_stock_cases = low_stock_cases + 1.0

    stock_rows = db.query(PresentStockDetail).order_by(
        PresentStockDetail.brand_name.asc(),
        PresentStockDetail.id.asc(),
    ).all()
    stock_summary = db.query(StockSummary).first()
    sales_history_map = _build_sales_history_map(db)

    stock_payload = [
        _stock_row_payload(
            row,
            _predict_required_stock(row, sales_history_map.get(row.id, [])),
            high_stock_cases,
        )
        for row in stock_rows
    ]
    zero_stock = [item for item in stock_payload if item["stock_level"] == "zero"]
    low_stock = [item for item in stock_payload if item["stock_level"] == "low"]
    high_stock = [item for item in stock_payload if item["stock_level"] == "high"]
    normal_stock = [item for item in stock_payload if item["stock_level"] == "normal"]

    low_stock_sorted = sorted(
        low_stock,
        key=lambda item: (
            item["coverage_ratio"] if item["coverage_ratio"] is not None else 10**9,
            -item["latest_sold_bottles"],
            item["brand_name"] or "",
            item["id"],
        ),
    )
    high_stock_sorted = sorted(
        high_stock,
        key=lambda item: (
            -(item["stock_gap_bottles"]),
            -(item["equivalent_cases"]),
            item["brand_name"] or "",
            item["id"],
        ),
    )
    zero_stock_sorted = sorted(
        zero_stock,
        key=lambda item: ((item["brand_name"] or ""), item["id"]),
    )
    top_value_stock = sorted(
        stock_payload,
        key=lambda item: (-_safe_float(item["total_amount"]), item["brand_name"] or "", item["id"]),
    )[:DEFAULT_RECENT_LIMIT]
    predictions_sorted = sorted(
        stock_payload,
        key=lambda item: (
            -_safe_int(item["predicted_required_bottles"]),
            -_safe_int(item["latest_sold_bottles"]),
            item["brand_name"] or "",
            item["id"],
        ),
    )

    total_stock_bottles = sum(_safe_int(item["total_bottles"]) for item in stock_payload)
    total_stock_equivalent_cases = round(sum(_safe_float(item["equivalent_cases"]) for item in stock_payload), 2)
    total_required_stock_bottles = sum(_safe_int(item["required_stock_bottles"]) for item in stock_payload)
    total_required_stock_cases = round(sum(_safe_float(item["required_stock_cases"]) for item in stock_payload), 2)
    total_shortage_bottles = sum(_safe_int(item["shortage_bottles"]) for item in stock_payload)
    total_stock_amount = (
        _safe_float(stock_summary.total_price_all_items)
        if stock_summary else
        sum(_safe_float(item["total_amount"]) for item in stock_payload)
    )

    latest_invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
    latest_invoice_totals = None
    if latest_invoice and latest_invoice.invoice_number:
        latest_invoice_totals = db.query(InvoiceTotals).filter(
            InvoiceTotals.invoice_number == latest_invoice.invoice_number
        ).first()

    latest_sell_report = db.query(SellReport).order_by(
        SellReport.report_date.desc(),
        SellReport.created_at.desc(),
    ).first()
    latest_sell_report_total = 0.0
    if latest_sell_report and latest_sell_report.report_date:
        latest_sell_report_total = _safe_float(db.query(
            func.coalesce(func.sum(SellReport.sell_amount), 0.0)
        ).filter(
            SellReport.report_date == latest_sell_report.report_date
        ).scalar())

    latest_finance = db.query(SellFinance).order_by(SellFinance.created_at.desc()).first()

    total_invoice_value = _safe_float(db.query(
        func.coalesce(func.sum(InvoiceTotals.total_invoice_value), 0.0)
    ).scalar())
    total_net_invoice_value = _safe_float(db.query(
        func.coalesce(func.sum(InvoiceTotals.net_invoice_value), 0.0)
    ).scalar())
    total_sell_amount = _safe_float(db.query(
        func.coalesce(func.sum(SellReport.sell_amount), 0.0)
    ).scalar())
    total_finance_balance = _safe_float(latest_finance.final_balance if latest_finance else 0.0)

    invoice_count = _safe_int(db.query(func.count(Invoice.id)).scalar())
    sell_report_days = _safe_int(db.query(func.count(func.distinct(SellReport.report_date))).scalar())
    finance_records = _safe_int(db.query(func.count(SellFinance.id)).scalar())

    recent_finance_rows = db.query(SellFinance).order_by(SellFinance.created_at.desc()).limit(DEFAULT_RECENT_LIMIT).all()
    recent_sell_report_rows = db.query(
        SellReport.report_date,
        func.count(SellReport.id),
        func.coalesce(func.sum(SellReport.sell_amount), 0.0),
        func.max(SellReport.created_at),
    ).group_by(SellReport.report_date).order_by(func.max(SellReport.created_at).desc()).limit(DEFAULT_RECENT_LIMIT).all()

    finance_messages = []
    if not latest_finance:
        finance_messages.append("No sell finance records created yet.")
    elif total_finance_balance < 0:
        finance_messages.append(
            f"Latest finance balance is negative: {total_finance_balance:.2f}."
        )
    else:
        finance_messages.append(
            f"Latest finance balance available: {total_finance_balance:.2f}."
        )

    if latest_invoice and latest_invoice_totals:
        finance_messages.append(
            f"Latest invoice {latest_invoice.invoice_number} value is "
            f"{_safe_float(latest_invoice_totals.total_invoice_value):.2f}."
        )
    if latest_sell_report and latest_sell_report.report_date:
        finance_messages.append(
            f"Latest sell report {latest_sell_report.report_date} total sell amount is "
            f"{latest_sell_report_total:.2f}."
        )

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "thresholds": {
            "low_stock_cases": low_stock_cases,
            "high_stock_cases": high_stock_cases,
        },
        "messages": {
            "low_stock": [_message_from_stock_payload(item) for item in low_stock_sorted[:DEFAULT_MESSAGE_LIMIT]],
            "high_stock": [
                (
                    f"{item['brand_name']} {item['pack_size_quantity_ml']}ml: stock {item['total_bottles']}, required {item['required_stock_bottles']}."
                    if _safe_int(item["predicted_required_bottles"]) > 0 else
                    f"{item['brand_name']} {item['pack_size_quantity_ml']}ml: extra stock, no recent demand."
                )
                for item in high_stock_sorted[:DEFAULT_MESSAGE_LIMIT]
            ],
            "finance": finance_messages,
        },
        "stock": {
            "summary": {
                "total_items": len(stock_payload),
                "zero_stock_count": len(zero_stock_sorted),
                "low_stock_count": len(low_stock_sorted),
                "normal_stock_count": len(normal_stock),
                "high_stock_count": len(high_stock_sorted),
                "total_stock_bottles": total_stock_bottles,
                "total_stock_equivalent_cases": total_stock_equivalent_cases,
                "total_required_stock_bottles": total_required_stock_bottles,
                "total_required_stock_cases": total_required_stock_cases,
                "total_shortage_bottles": total_shortage_bottles,
                "total_stock_amount": round(total_stock_amount, 2),
                "brands_with_recent_sales": len([item for item in stock_payload if _safe_int(item["predicted_required_bottles"]) > 0]),
                "last_updated_item_name": stock_summary.last_updated_item_name if stock_summary else "",
                "updated_at": stock_summary.updated_at.isoformat() if stock_summary and stock_summary.updated_at else None,
            },
            "predictions": predictions_sorted,
            "required_stock": predictions_sorted,
            "low_stock": low_stock_sorted,
            "high_stock": high_stock_sorted,
            "zero_stock": zero_stock_sorted,
            "top_value_stock": top_value_stock,
        },
        "finance": {
            "summary": {
                "invoice_count": invoice_count,
                "sell_report_days": sell_report_days,
                "finance_records": finance_records,
                "total_invoice_value": round(total_invoice_value, 2),
                "total_net_invoice_value": round(total_net_invoice_value, 2),
                "total_sell_amount": round(total_sell_amount, 2),
                "latest_final_balance": round(total_finance_balance, 2),
            },
            "latest_invoice": {
                "invoice_number": latest_invoice.invoice_number if latest_invoice else "",
                "invoice_date": latest_invoice.invoice_date if latest_invoice else "",
                "uploaded_by": latest_invoice.uploaded_by if latest_invoice else "",
                "uploaded_at": latest_invoice.uploaded_at.isoformat() if latest_invoice and latest_invoice.uploaded_at else None,
                "total_invoice_value": _safe_float(latest_invoice_totals.total_invoice_value) if latest_invoice_totals else 0.0,
                "net_invoice_value": _safe_float(latest_invoice_totals.net_invoice_value) if latest_invoice_totals else 0.0,
                "retailer_credit_balance": _safe_float(latest_invoice_totals.retailer_credit_balance) if latest_invoice_totals else 0.0,
            },
            "latest_sell_report": {
                "report_date": latest_sell_report.report_date if latest_sell_report else "",
                "created_by": latest_sell_report.created_by if latest_sell_report else "",
                "created_at": latest_sell_report.created_at.isoformat() if latest_sell_report and latest_sell_report.created_at else None,
                "total_sell_amount": round(latest_sell_report_total, 2),
            },
            "latest_finance": {
                "report_date": latest_finance.report_date if latest_finance else "",
                "total_sell_amount": _safe_float(latest_finance.total_sell_amount) if latest_finance else 0.0,
                "last_balance_amount": _safe_float(latest_finance.last_balance_amount) if latest_finance else 0.0,
                "total_amount": _safe_float(latest_finance.total_amount) if latest_finance else 0.0,
                "upi_phonepay": _safe_float(latest_finance.upi_phonepay) if latest_finance else 0.0,
                "cash": _safe_float(latest_finance.cash) if latest_finance else 0.0,
                "total_balance": _safe_float(latest_finance.total_balance) if latest_finance else 0.0,
                "total_outside_income": _safe_float(latest_finance.total_outside_income) if latest_finance else 0.0,
                "total_expenses": _safe_float(latest_finance.total_expenses) if latest_finance else 0.0,
                "final_balance": _safe_float(latest_finance.final_balance) if latest_finance else 0.0,
                "created_by": latest_finance.created_by if latest_finance else "",
                "updated_by": latest_finance.updated_by if latest_finance else "",
                "updated_at": latest_finance.updated_at.isoformat() if latest_finance and latest_finance.updated_at else None,
            },
            "recent_finance": [
                {
                    "report_date": row.report_date,
                    "total_sell_amount": _safe_float(row.total_sell_amount),
                    "total_amount": _safe_float(row.total_amount),
                    "final_balance": _safe_float(row.final_balance),
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in recent_finance_rows
            ],
            "recent_sell_reports": [
                {
                    "report_date": row[0],
                    "total_items": _safe_int(row[1]),
                    "total_sell_amount": _safe_float(row[2]),
                    "last_created_at": row[3].isoformat() if row[3] else None,
                }
                for row in recent_sell_report_rows
            ],
        },
    }

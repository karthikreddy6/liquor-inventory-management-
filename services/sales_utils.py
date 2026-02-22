from datetime import datetime

from sqlalchemy import func
from sqlalchemy.exc import OperationalError

from models import (
    Invoice,
    InvoiceItem,
    PriceListItem,
    SellFinance,
    SellFinanceCash,
    SellFinanceExpense,
    SellFinancePhonePay,
    SellReport,
)


def get_last_reports_by_stock(db):
    last_reports = {}
    try:
        rows = db.query(SellReport).order_by(SellReport.created_at.desc()).all()
        for r in rows:
            if r.stock_id not in last_reports:
                last_reports[r.stock_id] = r
    except OperationalError:
        return {}
    return last_reports


def get_previous_report(db, stock_id, before_dt):
    if before_dt is None:
        return None
    return db.query(SellReport).filter(
        SellReport.stock_id == stock_id,
        SellReport.created_at < before_dt
    ).order_by(SellReport.created_at.desc()).first()


def invoice_additions(db, stock, since_dt):
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


def total_bottles(cases, loose_bottles, pack_size_case):
    return int(cases or 0) * int(pack_size_case or 0) + int(loose_bottles or 0)


def compute_opening_and_additions(db, stock, last_report):
    if last_report:
        opening_cases = last_report.closing_cases
        opening_bottles = last_report.closing_bottles
        since_dt = last_report.created_at
        added_cases, added_bottles = invoice_additions(db, stock, since_dt)
    else:
        opening_cases = 0
        opening_bottles = 0
        added_cases, added_bottles = invoice_additions(db, stock, None)

    total_cases = int(opening_cases or 0) + int(added_cases or 0)
    opening_total_bottles = total_bottles(opening_cases, opening_bottles, stock.pack_size_case)
    added_total_bottles = total_bottles(added_cases, added_bottles, stock.pack_size_case)
    total_bottles_value = opening_total_bottles + added_total_bottles

    return opening_cases, opening_bottles, added_cases, added_bottles, total_cases, total_bottles_value


def build_mrp_map(db):
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


def get_total_sell_amount(db, report_date):
    total = db.query(func.coalesce(func.sum(SellReport.sell_amount), 0.0)).filter(
        SellReport.report_date == report_date
    ).scalar()
    try:
        return float(total or 0.0)
    except Exception:
        return 0.0


def get_last_finance_balance(db, exclude_finance_id=None):
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


def build_finance_payload(db, report_date):
    finance = db.query(SellFinance).filter(SellFinance.report_date == report_date).first()
    if not finance:
        return {
            "exists": False,
            "report_date": report_date,
            "total_sell_amount": 0.0,
            "last_balance_amount": 0.0,
            "total_amount": 0.0,
            "upi_phonepay": 0.0,
            "cash": 0.0,
            "total_balance": 0.0,
            "total_expenses": 0.0,
            "final_balance": 0.0,
            "phonepay_entries": [],
            "cash_entries": [],
            "expenses": []
        }

    phonepay_rows = db.query(SellFinancePhonePay).filter(
        SellFinancePhonePay.finance_id == finance.id
    ).all()
    cash_rows = db.query(SellFinanceCash).filter(
        SellFinanceCash.finance_id == finance.id
    ).all()
    expense_rows = db.query(SellFinanceExpense).filter(
        SellFinanceExpense.finance_id == finance.id
    ).all()

    return {
        "exists": True,
        "report_date": finance.report_date,
        "total_sell_amount": float(finance.total_sell_amount or 0.0),
        "last_balance_amount": float(finance.last_balance_amount or 0.0),
        "total_amount": float(finance.total_amount or 0.0),
        "upi_phonepay": float(finance.upi_phonepay or 0.0),
        "cash": float(finance.cash or 0.0),
        "total_balance": float(finance.total_balance or 0.0),
        "total_expenses": float(finance.total_expenses or 0.0),
        "final_balance": float(finance.final_balance or 0.0),
        "phonepay_entries": [
            {"date": r.txn_date, "amount": float(r.amount or 0.0)}
            for r in phonepay_rows
        ],
        "cash_entries": [
            {"date": r.txn_date, "amount": float(r.amount or 0.0)}
            for r in cash_rows
        ],
        "expenses": [
            {"name": r.name, "amount": float(r.amount or 0.0)}
            for r in expense_rows
        ]
    }


def parse_report_date(val):
    if not val:
        return None
    val = str(val).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(val, fmt).date()
        except Exception:
            continue
    return None


def to_float_amount(value):
    if value is None:
        return 0.0
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value:
            return 0.0
    return float(value)


def normalize_money_entries(
    entries,
    kind,
    min_allowed_dt,
    min_allowed_label,
    max_allowed_dt,
    max_allowed_label,
):
    cleaned = []
    total = 0.0
    for idx, entry in enumerate(entries, start=1):
        row = entry or {}
        raw_date = str(row.get("date") or row.get("txn_date") or "").strip()
        raw_amount = row.get("amount", "")

        if not raw_date and (raw_amount is None or str(raw_amount).strip() == ""):
            continue
        if not raw_date:
            return None, None, {"error": f"{kind} entry #{idx} date is required"}

        txn_dt = parse_report_date(raw_date)
        if not txn_dt:
            return None, None, {"error": f"invalid {kind} date format in entry #{idx}: {raw_date}"}
        if txn_dt < min_allowed_dt:
            return None, None, {
                "error": f"{kind} date must be on or after {min_allowed_label}: {min_allowed_dt.strftime('%Y-%m-%d')}"
            }
        if txn_dt > max_allowed_dt:
            return None, None, {
                "error": f"{kind} date must be on or before {max_allowed_label}: {max_allowed_dt.strftime('%Y-%m-%d')}"
            }

        try:
            amount = to_float_amount(raw_amount)
        except Exception:
            return None, None, {"error": f"{kind} entry #{idx} amount must be a number"}

        total += amount
        cleaned.append({
            "date": txn_dt.strftime("%Y-%m-%d"),
            "amount": amount
        })
    return cleaned, total, None

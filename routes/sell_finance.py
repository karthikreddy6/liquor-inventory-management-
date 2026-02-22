from flask import Blueprint, jsonify, request
from sqlalchemy import func

from auth import auth_required
from database import SessionLocal
from models import (
    Invoice,
    InvoiceTotals,
    SellFinance,
    SellFinanceCash,
    SellFinanceExpense,
    SellFinancePhonePay,
    SellReport,
)
from services.audit import log_action
from services.sales_utils import (
    get_last_finance_balance,
    get_total_sell_amount,
    normalize_money_entries,
    parse_report_date,
)

sell_finance_bp = Blueprint("sell_finance", __name__)


@sell_finance_bp.route("/seller/sell-finance", methods=["POST"])
@auth_required()
def create_sell_finance():
    payload = request.get_json(silent=True) or {}
    report_date = payload.get("report_date")
    upi_phonepay = payload.get("upi_phonepay", 0)
    cash = payload.get("cash", 0)
    expenses = payload.get("expenses", [])
    phonepay_entries = payload.get("phonepay_entries", [])
    cash_entries = payload.get("cash_entries", [])

    if not report_date:
        return {"error": "report_date is required"}, 400
    if not isinstance(expenses, list):
        return {"error": "expenses must be a list"}, 400
    if not isinstance(phonepay_entries, list):
        return {"error": "phonepay_entries must be a list"}, 400
    if not isinstance(cash_entries, list):
        return {"error": "cash_entries must be a list"}, 400

    db = SessionLocal()
    try:
        SellFinancePhonePay.__table__.create(bind=db.get_bind(), checkfirst=True)
        SellFinanceCash.__table__.create(bind=db.get_bind(), checkfirst=True)

        latest_invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
        if not latest_invoice:
            return {"error": "no invoices found"}, 400
        latest_invoice_date = latest_invoice.invoice_date
        latest_invoice_dt = parse_report_date(latest_invoice_date)
        if not latest_invoice_dt:
            return {"error": "invalid latest invoice date format"}, 400
        report_dt = parse_report_date(report_date)
        if not report_dt:
            return {"error": "invalid report_date format"}, 400
        if report_dt < latest_invoice_dt:
            return {"error": "report_date must be on or after last invoice date"}, 400

        sell_report_exists = db.query(SellReport).filter(
            SellReport.report_date == report_date
        ).first()
        if not sell_report_exists:
            return {"error": "sell report not found for this date"}, 404

        finance = db.query(SellFinance).filter(
            SellFinance.report_date == report_date
        ).first()

        previous_sell_report = db.query(SellReport).filter(
            SellReport.report_date < report_date
        ).order_by(SellReport.report_date.desc()).first()
        min_allowed_dt = parse_report_date(previous_sell_report.report_date) if previous_sell_report else report_dt
        if not min_allowed_dt:
            min_allowed_dt = report_dt
            min_allowed_label = "selected sell report date"
        else:
            min_allowed_label = "previous sell report date" if previous_sell_report else "selected sell report date"

        last_balance_amount = get_last_finance_balance(db, finance.id if finance else None)
        total_sell_amount = get_total_sell_amount(db, report_date)

        if not phonepay_entries and (upi_phonepay not in (None, "", 0, 0.0, "0", "0.0")):
            phonepay_entries = [{"date": report_date, "amount": upi_phonepay}]
        if not cash_entries and (cash not in (None, "", 0, 0.0, "0", "0.0")):
            cash_entries = [{"date": report_date, "amount": cash}]

        cleaned_phonepay_entries, phonepay_total, phonepay_err = normalize_money_entries(
            phonepay_entries,
            "phonepay",
            min_allowed_dt,
            min_allowed_label,
            report_dt,
            "selected sell report date",
        )
        if phonepay_err:
            return phonepay_err, 400
        cleaned_cash_entries, cash_total, cash_err = normalize_money_entries(
            cash_entries,
            "cash",
            min_allowed_dt,
            min_allowed_label,
            report_dt,
            "selected sell report date",
        )
        if cash_err:
            return cash_err, 400

        upi_phonepay = phonepay_total
        cash = cash_total

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
            db.query(SellFinancePhonePay).filter(
                SellFinancePhonePay.finance_id == finance.id
            ).delete()
            db.query(SellFinanceCash).filter(
                SellFinanceCash.finance_id == finance.id
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
        for entry in cleaned_phonepay_entries:
            db.add(SellFinancePhonePay(
                finance_id=finance.id,
                txn_date=entry["date"],
                amount=entry["amount"]
            ))
        for entry in cleaned_cash_entries:
            db.add(SellFinanceCash(
                finance_id=finance.id,
                txn_date=entry["date"],
                amount=entry["amount"]
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
            "phonepay_entries": cleaned_phonepay_entries,
            "cash_entries": cleaned_cash_entries,
            "expenses": cleaned_expenses
        })
    finally:
        db.close()


@sell_finance_bp.route("/seller/sell-finance/prepare", methods=["GET"])
@auth_required()
def prepare_sell_finance():
    report_date = request.args.get("report_date")
    if not report_date:
        return {"error": "report_date is required"}, 400

    db = SessionLocal()
    try:
        SellFinancePhonePay.__table__.create(bind=db.get_bind(), checkfirst=True)
        SellFinanceCash.__table__.create(bind=db.get_bind(), checkfirst=True)

        latest_invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
        latest_invoice_date = latest_invoice.invoice_date if latest_invoice else ""
        report_dt = parse_report_date(report_date)
        if not report_dt:
            return {"error": "invalid report_date format"}, 400
        sell_report_exists = db.query(SellReport).filter(
            SellReport.report_date == report_date
        ).first()
        if not sell_report_exists:
            return {"error": "sell report not found for this date"}, 404
        previous_sell_report = db.query(SellReport).filter(
            SellReport.report_date < report_date
        ).order_by(SellReport.report_date.desc()).first()
        min_allowed_date = previous_sell_report.report_date if previous_sell_report else report_date

        finance = db.query(SellFinance).filter(
            SellFinance.report_date == report_date
        ).first()
        phonepay_entries = []
        cash_entries = []
        if finance:
            phonepay_rows = db.query(SellFinancePhonePay).filter(
                SellFinancePhonePay.finance_id == finance.id
            ).all()
            phonepay_entries = [
                {"date": r.txn_date, "amount": float(r.amount or 0.0)}
                for r in phonepay_rows
            ]
            cash_rows = db.query(SellFinanceCash).filter(
                SellFinanceCash.finance_id == finance.id
            ).all()
            cash_entries = [
                {"date": r.txn_date, "amount": float(r.amount or 0.0)}
                for r in cash_rows
            ]
            if not phonepay_entries and float(finance.upi_phonepay or 0.0) != 0.0:
                phonepay_entries = [{"date": report_date, "amount": float(finance.upi_phonepay or 0.0)}]
            if not cash_entries and float(finance.cash or 0.0) != 0.0:
                cash_entries = [{"date": report_date, "amount": float(finance.cash or 0.0)}]

        expenses = []
        if finance:
            expense_rows = db.query(SellFinanceExpense).filter(
                SellFinanceExpense.finance_id == finance.id
            ).all()
            expenses = [
                {"name": r.name, "amount": float(r.amount or 0.0)}
                for r in expense_rows
            ]

        last_balance_amount = get_last_finance_balance(db, finance.id if finance else None)
        total_sell_amount = get_total_sell_amount(db, report_date)
        total_amount = float(total_sell_amount) + float(last_balance_amount)

        return jsonify({
            "report_date": report_date,
            "total_sell_amount": total_sell_amount,
            "last_balance_amount": last_balance_amount,
            "total_amount": total_amount,
            "existing_finance": bool(finance),
            "upi_phonepay": float(finance.upi_phonepay or 0.0) if finance else 0.0,
            "cash": float(finance.cash or 0.0) if finance else 0.0,
            "total_balance": float(finance.total_balance or 0.0) if finance else 0.0,
            "total_expenses": float(finance.total_expenses or 0.0) if finance else 0.0,
            "final_balance": float(finance.final_balance or 0.0) if finance else 0.0,
            "phonepay_entries": phonepay_entries,
            "cash_entries": cash_entries,
            "expenses": expenses,
            "latest_invoice_date": latest_invoice_date,
            "allowed_entry_date_from": min_allowed_date,
            "allowed_entry_date_to": report_date
        })
    finally:
        db.close()


@sell_finance_bp.route("/seller/sell-finance/overview", methods=["GET"])
@auth_required()
def sell_finance_overview():
    db = SessionLocal()
    try:
        latest_invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
        latest_invoice_totals = None
        if latest_invoice and latest_invoice.invoice_number:
            latest_invoice_totals = db.query(InvoiceTotals).filter(
                InvoiceTotals.invoice_number == latest_invoice.invoice_number
            ).first()
        total_invoice_value_all = float(db.query(
            func.coalesce(func.sum(InvoiceTotals.total_invoice_value), 0.0)
        ).scalar() or 0.0)
        total_net_invoice_value_all = float(db.query(
            func.coalesce(func.sum(InvoiceTotals.net_invoice_value), 0.0)
        ).scalar() or 0.0)
        total_special_excise_cess_all = float(db.query(
            func.coalesce(func.sum(InvoiceTotals.special_excise_cess), 0.0)
        ).scalar() or 0.0)
        total_tcs_all = float(db.query(
            func.coalesce(func.sum(InvoiceTotals.tcs), 0.0)
        ).scalar() or 0.0)

        invoice_rows = db.query(Invoice).order_by(Invoice.id.desc()).all()
        invoice_numbers = [i.invoice_number for i in invoice_rows if i.invoice_number]
        totals_map = {}
        if invoice_numbers:
            totals_rows = db.query(InvoiceTotals).filter(
                InvoiceTotals.invoice_number.in_(invoice_numbers)
            ).all()
            for t in totals_rows:
                totals_map[t.invoice_number] = t

        invoices_payload = []
        for inv in invoice_rows:
            tot = totals_map.get(inv.invoice_number)
            invoices_payload.append({
                "invoice_number": inv.invoice_number,
                "invoice_date": inv.invoice_date,
                "uploaded_by": inv.uploaded_by or "",
                "uploaded_at": inv.uploaded_at.isoformat() if inv.uploaded_at else "",
                "net_invoice_value": float(tot.net_invoice_value or 0.0) if tot else 0.0,
                "special_excise_cess": float(tot.special_excise_cess or 0.0) if tot else 0.0,
                "tcs": float(tot.tcs or 0.0) if tot else 0.0,
                "total_invoice_value": float(tot.total_invoice_value or 0.0) if tot else 0.0,
                "retailer_credit_balance": float(tot.retailer_credit_balance or 0.0) if tot else 0.0
            })

        latest_sell_report = db.query(SellReport).order_by(SellReport.created_at.desc()).first()
        total_sell_amount_all = float(db.query(
            func.coalesce(func.sum(SellReport.sell_amount), 0.0)
        ).scalar() or 0.0)
        latest_sell_report_total = 0.0
        if latest_sell_report and latest_sell_report.report_date:
            latest_sell_report_total = float(db.query(
                func.coalesce(func.sum(SellReport.sell_amount), 0.0)
            ).filter(
                SellReport.report_date == latest_sell_report.report_date
            ).scalar() or 0.0)
        sell_report_rows = db.query(
            SellReport.report_date,
            func.count(SellReport.id),
            func.coalesce(func.sum(SellReport.sell_amount), 0.0),
            func.max(SellReport.created_at),
        ).group_by(SellReport.report_date).order_by(func.max(SellReport.created_at).desc()).all()

        finance_rows = db.query(SellFinance).order_by(SellFinance.created_at.desc()).all()
        finance_ids = [f.id for f in finance_rows]

        expenses_map = {}
        phonepay_map = {}
        cash_map = {}
        if finance_ids:
            exp_rows = db.query(SellFinanceExpense).filter(
                SellFinanceExpense.finance_id.in_(finance_ids)
            ).all()
            pp_rows = db.query(SellFinancePhonePay).filter(
                SellFinancePhonePay.finance_id.in_(finance_ids)
            ).all()
            cash_rows = db.query(SellFinanceCash).filter(
                SellFinanceCash.finance_id.in_(finance_ids)
            ).all()

            for r in exp_rows:
                expenses_map.setdefault(r.finance_id, []).append({
                    "name": r.name,
                    "amount": float(r.amount or 0.0)
                })
            for r in pp_rows:
                phonepay_map.setdefault(r.finance_id, []).append({
                    "date": r.txn_date,
                    "amount": float(r.amount or 0.0)
                })
            for r in cash_rows:
                cash_map.setdefault(r.finance_id, []).append({
                    "date": r.txn_date,
                    "amount": float(r.amount or 0.0)
                })

        finance_payload = []
        for f in finance_rows:
            phonepay_entries = phonepay_map.get(f.id, [])
            cash_entries = cash_map.get(f.id, [])
            if not phonepay_entries and float(f.upi_phonepay or 0.0) != 0.0:
                phonepay_entries = [{"date": f.report_date, "amount": float(f.upi_phonepay or 0.0)}]
            if not cash_entries and float(f.cash or 0.0) != 0.0:
                cash_entries = [{"date": f.report_date, "amount": float(f.cash or 0.0)}]

            finance_payload.append({
                "report_date": f.report_date,
                "total_sell_amount": float(f.total_sell_amount or 0.0),
                "last_balance_amount": float(f.last_balance_amount or 0.0),
                "total_amount": float(f.total_amount or 0.0),
                "upi_phonepay": float(f.upi_phonepay or 0.0),
                "cash": float(f.cash or 0.0),
                "total_balance": float(f.total_balance or 0.0),
                "total_expenses": float(f.total_expenses or 0.0),
                "final_balance": float(f.final_balance or 0.0),
                "created_by": f.created_by,
                "updated_by": f.updated_by,
                "created_at": f.created_at.isoformat() if f.created_at else None,
                "updated_at": f.updated_at.isoformat() if f.updated_at else None,
                "phonepay_entries": phonepay_entries,
                "cash_entries": cash_entries,
                "expenses": expenses_map.get(f.id, []),
            })

        return jsonify({
            "totals": {
                "all_invoices_total_invoice_value": total_invoice_value_all,
                "all_invoices_net_invoice_value": total_net_invoice_value_all,
                "all_invoices_special_excise_cess": total_special_excise_cess_all,
                "all_invoices_tcs": total_tcs_all,
                "all_sell_amount": total_sell_amount_all
            },
            "latest_invoice": {
                "invoice_number": latest_invoice.invoice_number if latest_invoice else "",
                "invoice_date": latest_invoice.invoice_date if latest_invoice else "",
                "uploaded_by": latest_invoice.uploaded_by if latest_invoice else "",
                "uploaded_at": latest_invoice.uploaded_at.isoformat() if latest_invoice and latest_invoice.uploaded_at else "",
                "net_invoice_value": float(latest_invoice_totals.net_invoice_value or 0.0) if latest_invoice_totals else 0.0,
                "special_excise_cess": float(latest_invoice_totals.special_excise_cess or 0.0) if latest_invoice_totals else 0.0,
                "tcs": float(latest_invoice_totals.tcs or 0.0) if latest_invoice_totals else 0.0,
                "total_invoice_value": total_invoice_value_all,
                "retailer_credit_balance": float(latest_invoice_totals.retailer_credit_balance or 0.0) if latest_invoice_totals else 0.0
            },
            "invoices": invoices_payload,
            "latest_sell_report": {
                "report_date": latest_sell_report.report_date if latest_sell_report else "",
                "created_by": latest_sell_report.created_by if latest_sell_report else "",
                "created_at": latest_sell_report.created_at.isoformat() if latest_sell_report and latest_sell_report.created_at else "",
                "sell_amount": total_sell_amount_all if latest_sell_report else 0.0,
                "latest_report_sell_amount": latest_sell_report_total if latest_sell_report else 0.0
            },
            "sell_reports": [
                {
                    "report_date": r[0],
                    "total_items": int(r[1] or 0),
                    "total_sell_amount": float(r[2] or 0.0),
                    "last_created_at": r[3].isoformat() if r[3] else None
                }
                for r in sell_report_rows
            ],
            "finance": finance_payload
        })
    finally:
        db.close()

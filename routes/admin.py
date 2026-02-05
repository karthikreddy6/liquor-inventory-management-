from flask import Blueprint, jsonify, Response, send_file, request
from sqlalchemy import text, func
import time
import os
from database import SessionLocal
from models import (
    Invoice,
    InvoiceItem,
    InvoiceTotals,
    PresentStockDetail,
    StockSummary,
    SellReport,
    SellFinance,
    SellFinanceExpense,
    PriceListItem,
    AuditLog,
    UserLogin,
)
from auth import jwt_required
from config import APP_START_TIME, ADMIN_USER, ADMIN_PASS
from services.pdf_export import write_invoice_pdf, write_sell_report_pdf
from functools import wraps
from services.audit import log_action, update_last_login

admin_bp = Blueprint("admin", __name__)

def admin_or_staff_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if auth:
            if auth.username == ADMIN_USER and auth.password == ADMIN_PASS:
                request.user = {"username": auth.username, "role": "admin"}
                request.auth_mode = "basic"
                db = SessionLocal()
                try:
                    update_last_login(db, request.user)
                    log_action(db, request.user, "login", "admin", auth.username)
                    db.commit()
                finally:
                    db.close()
                return fn(*args, **kwargs)
            return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="Admin"'})
        request.auth_mode = "jwt"
        return jwt_required(roles=["owner", "supervisor"])(fn)(*args, **kwargs)
    return wrapper


def admin_basic_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USER or auth.password != ADMIN_PASS:
            return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="Admin"'})
        request.user = {"username": auth.username, "role": "admin"}
        request.auth_mode = "basic"
        db = SessionLocal()
        try:
            update_last_login(db, request.user)
            log_action(db, request.user, "login", "admin", auth.username)
            db.commit()
        finally:
            db.close()
        return fn(*args, **kwargs)
    return wrapper

@admin_bp.route("/admin/status", methods=["GET"])
@admin_or_staff_required
def admin_status():
    db_ok = False
    db_error = None
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        db_error = str(e)
    finally:
        db.close()

    return jsonify({
        "status": "ok" if db_ok else "degraded",
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_seconds": int(time.time() - APP_START_TIME),
        "db_ok": db_ok,
        "db_error": db_error
    })

@admin_bp.route("/admin/db-summary", methods=["GET"])
@admin_or_staff_required
def admin_db_summary():
    db = SessionLocal()
    try:
        invoice_count = db.query(Invoice).count()
        item_count = db.query(InvoiceItem).count()
        stock_count = db.query(PresentStockDetail).count()
        latest_invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
        summary = db.query(StockSummary).first()
        mrp_map = {}
        for r in db.query(PriceListItem).all():
            key = (str(r.brand_number or "").strip(), str(r.pack_type or "").strip(), int(r.volume_ml or 0))
            if key not in mrp_map:
                mrp_map[key] = float(r.mrp or 0.0)
        total_stock_mrp_value = 0.0
        stocks = db.query(PresentStockDetail).all()
        for s in stocks:
            key = (str(s.brand_number or "").strip(), str(s.pack_type or "").strip(), int(s.pack_size_quantity_ml or 0))
            mrp = mrp_map.get(key)
            if mrp is None:
                continue
            total_stock_mrp_value += float(mrp) * float(s.total_bottles or 0)

        return jsonify({
            "invoice_count": invoice_count,
            "item_count": item_count,
            "stock_count": stock_count,
            "latest_invoice_date": latest_invoice.invoice_date if latest_invoice else None,
            "latest_invoice_number": latest_invoice.invoice_number if latest_invoice else None,
            "stock_summary_updated_at": summary.updated_at.isoformat() if summary and summary.updated_at else None
        })
    finally:
        db.close()

@admin_bp.route("/admin", methods=["GET"])
@admin_or_staff_required
def admin_dashboard():
    db_ok = False
    db_error = None
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        db_ok = True

        invoice_count = db.query(Invoice).count()
        item_count = db.query(InvoiceItem).count()
        stock_count = db.query(PresentStockDetail).count()
        latest_invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
        summary = db.query(StockSummary).first()

        # Calculate MRP value (logic from summary)
        mrp_map = {}
        for r in db.query(PriceListItem).all():
            key = (str(r.brand_number or "").strip(), str(r.pack_type or "").strip(), int(r.volume_ml or 0))
            if key not in mrp_map:
                mrp_map[key] = float(r.mrp or 0.0)
        total_stock_mrp_value = 0.0
        stocks = db.query(PresentStockDetail).all()
        for s in stocks:
            key = (str(s.brand_number or "").strip(), str(s.pack_type or "").strip(), int(s.pack_size_quantity_ml or 0))
            mrp = mrp_map.get(key)
            if mrp:
                total_stock_mrp_value += float(mrp) * float(s.total_bottles or 0)

    except Exception as e:
        db_error = str(e)
        invoice_count = 0
        item_count = 0
        stock_count = 0
        latest_invoice = None
        summary = None
        total_stock_mrp_value = 0.0
    finally:
        db.close()

    server_time = time.strftime("%Y-%m-%d %H:%M:%S")
    uptime_seconds = int(time.time() - APP_START_TIME)
    latest_invoice_date = latest_invoice.invoice_date if latest_invoice else "N/A"
    latest_invoice_number = latest_invoice.invoice_number if latest_invoice else "N/A"
    stock_summary_updated_at = (
        summary.updated_at.isoformat() if summary and summary.updated_at else "N/A"
    )

    auth_mode = getattr(request, "auth_mode", "jwt")
    auth_badge = "Basic Auth" if auth_mode == "basic" else "JWT Bearer"

    # Support JSON for the React Frontend
    if request.headers.get("Accept") == "application/json" or request.args.get("format") == "json":
        return jsonify({
            "status": "ok" if db_ok else "error",
            "auth_mode": auth_mode,
            "invoice_count": invoice_count,
            "item_count": item_count,
            "stock_count": stock_count,
            "latest_invoice_date": latest_invoice_date,
            "latest_invoice_number": latest_invoice_number,
            "total_stock_mrp_value": total_stock_mrp_value,
            "server_time": server_time,
            "uptime_seconds": uptime_seconds
        })

    html = f"""
<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\"/>
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>
    <title>Admin Dashboard</title>
    <style>
      :root {{
        --bg: #f6f1e7;
        --ink: #1c1b19;
        --accent: #0f766e;
        --card: #ffffff;
        --muted: #6b6b6b;
        --border: #e5e0d8;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: "Georgia", "Times New Roman", serif;
        background: radial-gradient(circle at top left, #efe7d6, #f6f1e7 45%, #f2efe9 100%);
        color: var(--ink);
      }}
      header {{
        padding: 24px 24px 8px 24px;
      }}
      .badge {{
        display: inline-block;
        padding: 4px 10px;
        border-radius: 999px;
        font-size: 12px;
        background: #e0f2fe;
        color: #075985;
        border: 1px solid #bae6fd;
        margin-left: 8px;
      }}
      h1 {{
        margin: 0 0 6px 0;
        letter-spacing: 0.5px;
      }}
      .sub {{
        color: var(--muted);
        font-size: 14px;
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 16px;
        padding: 16px 24px 32px 24px;
      }}
      .card {{
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 14px;
        padding: 16px;
        box-shadow: 0 8px 22px rgba(0,0,0,0.06);
      }}
      .label {{
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 1px;
        color: var(--muted);
        margin-bottom: 6px;
      }}
      .value {{
        font-size: 22px;
        font-weight: 600;
      }}
      .status {{
        display: inline-block;
        padding: 4px 10px;
        border-radius: 999px;
        font-size: 12px;
        background: {("#d1fae5" if db_ok else "#fee2e2")};
        color: {("#065f46" if db_ok else "#991b1b")};
        border: 1px solid {("#a7f3d0" if db_ok else "#fecaca")};
      }}
      .muted {{
        color: var(--muted);
        font-size: 13px;
      }}
      .links {{
        padding: 0 24px 24px 24px;
      }}
      .links a {{
        color: var(--accent);
        text-decoration: none;
        margin-right: 16px;
        font-weight: 600;
      }}
      .links a:hover {{
        text-decoration: underline;
      }}
    </style>
  </head>
  <body>
    <header>
      <h1>Admin Dashboard <span class=\"badge\">{auth_badge}</span></h1>
      <div class=\"sub\">Server time: {server_time} - Uptime: {uptime_seconds}s - DB: <span class=\"status\">{("OK" if db_ok else "ERROR")}</span></div>
      {f'<div class="muted">DB error: {db_error}</div>' if db_error else ''}
    </header>
    <section class=\"grid\">
      <div class=\"card\">
        <div class=\"label\">Invoices</div>
        <div class=\"value\">{invoice_count}</div>
      </div>
      <div class=\"card\">
        <div class=\"label\">Invoice Items</div>
        <div class=\"value\">{item_count}</div>
      </div>
      <div class=\"card\">
        <div class=\"label\">Stock Items</div>
        <div class=\"value\">{stock_count}</div>
      </div>
      <div class=\"card\">
        <div class=\"label\">Latest Invoice</div>
        <div class=\"value\">{latest_invoice_number}</div>
        <div class=\"muted\">{latest_invoice_date}</div>
      </div>
      <div class=\"card\">
        <div class=\"label\">Stock Summary Updated</div>
        <div class=\"value\">{stock_summary_updated_at}</div>
      </div>
      <div class=\"card\">
        <div class=\"label\">Total Stock Value (MRP)</div>
        <div class=\"value\">{total_stock_mrp_value:.2f}</div>
      </div>
      <div class=\"card\">
        <div class=\"label\">Technical Reference</div>
        <div class=\"muted\">JWT (Owner/Supervisor):</div>
        <div class=\"value\">Authorization: Bearer &lt;token&gt;</div>
        <div class=\"muted\" style=\"margin-top:8px;\">Basic (Admin):</div>
        <div class=\"value\">Authorization: Basic base64(admin:admin123)</div>
      </div>
    </section>
    <div class=\"links\">
      <a href=\"/admin/status\">JSON Status</a>
      <a href=\"/admin/db-summary\">JSON DB Summary</a>
    </div>
  </body>
</html>
"""
    return Response(html, mimetype="text/html")


@admin_bp.route("/dashboard/summary", methods=["GET"])
@jwt_required(roles=["owner", "supervisor"])
def dashboard_summary():
    db = SessionLocal()
    try:
        last_finance = db.query(SellFinance).order_by(SellFinance.created_at.desc()).first()
        last_uncleared_amount = float(last_finance.final_balance or 0.0) if last_finance else 0.0

        last_invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
        last_invoice_date = last_invoice.invoice_date if last_invoice else ""
        last_invoice_number = last_invoice.invoice_number if last_invoice else ""

        last_invoice_value = 0.0
        last_retailer_credit = 0.0
        if last_invoice:
            totals = db.query(InvoiceTotals).filter(
                InvoiceTotals.invoice_number == last_invoice.invoice_number
            ).first()
            if totals:
                last_invoice_value = float(totals.net_invoice_value or 0.0)
                last_retailer_credit = float(totals.retailer_credit_balance or 0.0)

        total_present_stock = db.query(
            func.coalesce(func.sum(PresentStockDetail.total_cases), 0)
        ).scalar()
        total_present_stock = int(total_present_stock or 0)

        summary = db.query(StockSummary).first()
        total_present_stock_mrp_value = float(summary.total_price_all_items or 0.0) if summary else 0.0

        last_report = db.query(SellReport).order_by(SellReport.created_at.desc()).first()
        last_report_date = last_report.report_date if last_report else ""
        last_sell_report_value = 0.0
        if last_report_date:
            last_sell_report_value = db.query(
                func.coalesce(func.sum(SellReport.sell_amount), 0.0)
            ).filter(SellReport.report_date == last_report_date).scalar()

        return jsonify({
            "last_uncleared_amount": last_uncleared_amount,
            "last_invoice_date": last_invoice_date,
            "last_invoice_number": last_invoice_number,
            "last_invoice_value": last_invoice_value,
            "last_invoice_retailer_credit_balance": last_retailer_credit,
            "total_present_stock": total_present_stock,
            "total_present_stock_mrp_value": total_present_stock_mrp_value,
            "last_sell_report_date": last_report_date,
            "last_sell_report_value": float(last_sell_report_value or 0.0)
        })
    finally:
        db.close()


@admin_bp.route("/reports/invoices", methods=["GET"])
@jwt_required(roles=["owner", "supervisor"])
def list_invoices():
    db = SessionLocal()
    try:
        rows = db.query(Invoice).order_by(Invoice.id.desc()).all()
        items = []
        for r in rows:
            uploaded_at = None
            if r.uploaded_at:
                uploaded_at = r.uploaded_at.isoformat()
            elif r.created_at:
                uploaded_at = r.created_at.isoformat()
            items.append({
                "invoice_number": r.invoice_number,
                "invoice_date": r.invoice_date,
                "uploaded_at": uploaded_at,
                "uploaded_by": r.uploaded_by or "unknown",
                "retailer_code": r.retailer_code
            })
        return jsonify({
            "count": len(items),
            "items": items
        })
    finally:
        db.close()


@admin_bp.route("/reports/sell-reports", methods=["GET"])
@jwt_required(roles=["owner", "supervisor"])
def list_sell_reports():
    db = SessionLocal()
    try:
        rows = db.query(SellReport).order_by(SellReport.created_at.desc()).all()
        finances = db.query(SellFinance).order_by(SellFinance.created_at.desc()).all()
        finance_map = {f.report_date: f for f in finances}
        summary = {}
        for r in rows:
            key = r.report_date or "unknown"
            fin = finance_map.get(r.report_date)
            if key not in summary:
                summary[key] = {
                    "report_date": r.report_date,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "created_by": r.created_by or "unknown",
                    "total_items": 0,
                    "edited_by": r.edited_by or "",
                    "edited_at": r.edited_at.isoformat() if r.edited_at else None,
                    "edit_count": r.edit_count or 0,
                    "finance": {
                        "total_sell_amount": fin.total_sell_amount,
                        "last_balance_amount": fin.last_balance_amount,
                        "total_amount": fin.total_amount,
                        "upi_phonepay": fin.upi_phonepay,
                        "cash": fin.cash,
                        "total_balance": fin.total_balance,
                        "total_expenses": fin.total_expenses,
                        "final_balance": fin.final_balance,
                        "created_by": fin.created_by,
                        "updated_by": fin.updated_by,
                        "created_at": fin.created_at.isoformat() if fin.created_at else None,
                        "updated_at": fin.updated_at.isoformat() if fin.updated_at else None
                    } if fin else None
                }
            summary[key]["total_items"] += 1
            if r.edit_count and not summary[key]["edited_by"]:
                summary[key]["edited_by"] = r.edited_by or "unknown"
                summary[key]["edited_at"] = r.edited_at.isoformat() if r.edited_at else None
                summary[key]["edit_count"] = r.edit_count or 0

        return jsonify({
            "count": len(summary),
            "items": list(summary.values())
        })
    finally:
        db.close()


@admin_bp.route("/admin/audit-logs", methods=["GET"])
@admin_basic_required
def admin_audit_logs():
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(200).all()
        items = []
        for r in rows:
            items.append({
                "username": r.username,
                "role": r.role,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "details": r.details,
                "created_at": r.created_at.isoformat() if r.created_at else None
            })
        return jsonify({"count": len(items), "items": items})
    finally:
        db.close()


@admin_bp.route("/admin/user-logins", methods=["GET"])
@admin_basic_required
def admin_user_logins():
    db = SessionLocal()
    try:
        rows = db.query(UserLogin).order_by(UserLogin.last_login_at.desc()).all()
        items = []
        for r in rows:
            items.append({
                "username": r.username,
                "role": r.role,
                "last_login_at": r.last_login_at.isoformat() if r.last_login_at else None
            })
        return jsonify({"count": len(items), "items": items})
    finally:
        db.close()


@admin_bp.route("/admin/reports/sell-reports/<report_date>", methods=["DELETE"])
@admin_basic_required
def admin_delete_sell_report(report_date):
    db = SessionLocal()
    try:
        rows = db.query(SellReport).filter(SellReport.report_date == report_date).all()
        if not rows:
            return {"error": "sell report not found"}, 404
        for r in rows:
            db.delete(r)
        log_action(db, request.user, "delete_sell_report", "sell_report", report_date)
        db.commit()
        return jsonify({"status": "ok", "deleted": len(rows)})
    finally:
        db.close()


@admin_bp.route("/admin/invoices/<invoice_number>", methods=["DELETE"])
@admin_basic_required
def admin_delete_invoice(invoice_number):
    db = SessionLocal()
    try:
        invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number).first()
        if not invoice:
            return {"error": "invoice not found"}, 404
        db.query(InvoiceItem).filter(InvoiceItem.invoice_number == invoice_number).delete()
        db.query(InvoiceTotals).filter(InvoiceTotals.invoice_number == invoice_number).delete()
        db.delete(invoice)
        log_action(db, request.user, "delete_invoice", "invoice", invoice_number)
        db.commit()
        return jsonify({"status": "ok"})
    finally:
        db.close()


@admin_bp.route("/admin/sell-finance/<report_date>", methods=["DELETE"])
@admin_basic_required
def admin_delete_sell_finance(report_date):
    db = SessionLocal()
    try:
        fin = db.query(SellFinance).filter(SellFinance.report_date == report_date).first()
        if not fin:
            return {"error": "sell finance not found"}, 404
        db.query(SellFinanceExpense).filter(SellFinanceExpense.finance_id == fin.id).delete()
        db.delete(fin)
        log_action(db, request.user, "delete_sell_finance", "sell_finance", report_date)
        db.commit()
        return jsonify({"status": "ok"})
    finally:
        db.close()


@admin_bp.route("/admin/stock/<int:stock_id>", methods=["PATCH"])
@admin_basic_required
def admin_update_stock(stock_id):
    payload = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        stock = db.query(PresentStockDetail).filter(PresentStockDetail.id == stock_id).first()
        if not stock:
            return {"error": "stock not found"}, 404

        fields = ["total_cases", "total_bottles", "rate_per_case", "unit_rate_per_bottle", "total_amount"]
        for f in fields:
            if f in payload:
                setattr(stock, f, payload.get(f))
        log_action(db, request.user, "edit_stock", "stock", stock_id, details=str(payload))
        db.commit()
        return jsonify({"status": "ok"})
    finally:
        db.close()


@admin_bp.route("/reports/invoices/<invoice_number>/pdf", methods=["GET"])
@jwt_required(roles=["owner", "supervisor"])
def invoice_pdf(invoice_number):
    db = SessionLocal()
    try:
        invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number).first()
        if not invoice:
            return {"error": "invoice not found"}, 404

        totals = db.query(InvoiceTotals).filter(
            InvoiceTotals.invoice_number == invoice_number
        ).first()
        items = db.query(InvoiceItem).filter(
            InvoiceItem.invoice_number == invoice_number
        ).order_by(InvoiceItem.sl_no.asc()).all()

        meta_rows = [
            ["Invoice Number", invoice.invoice_number],
            ["Invoice Date", invoice.invoice_date],
            ["Retailer", f"{invoice.retailer_name} ({invoice.retailer_code})"],
            ["Licensee PAN", invoice.licensee_pan],
        ]
        totals_rows = []
        if totals:
            totals_rows = [
                ["Invoice Value", totals.invoice_value],
                ["MRP Round Off", totals.mrp_round_off],
                ["Net Invoice Value", totals.net_invoice_value],
                ["Retailer Credit Balance", totals.retailer_credit_balance],
            ]
        items_rows = [["#", "Brand", "Pack", "Size", "Cases", "Bottles", "Total"]]
        for it in items:
            items_rows.append([
                it.sl_no,
                it.brand_name or "",
                it.pack_type or "",
                f"{it.pack_size_case}/{it.pack_size_quantity_ml}ml",
                it.cases_delivered,
                it.bottles_delivered,
                it.total_amount
            ])

        out_dir = os.path.join("requested_pdf", "invoices")
        filename = f"{invoice.invoice_number}.pdf"
        out_path = os.path.join(out_dir, filename)
        write_invoice_pdf(out_path, meta_rows, items_rows, totals_rows, title="Invoice Report")
        return send_file(out_path, as_attachment=True, download_name=filename)
    finally:
        db.close()


@admin_bp.route("/reports/sell-reports/<report_date>/pdf", methods=["GET"])
@jwt_required(roles=["owner", "supervisor"])
def sell_report_pdf(report_date):
    db = SessionLocal()
    try:
        rows = db.query(SellReport).filter(
            SellReport.report_date == report_date
        ).order_by(SellReport.stock_id.asc()).all()
        if not rows:
            return {"error": "sell report not found"}, 404

        fin = db.query(SellFinance).filter(
            SellFinance.report_date == report_date
        ).first()
        expenses = []
        if fin:
            expenses = db.query(SellFinanceExpense).filter(
                SellFinanceExpense.finance_id == fin.id
            ).all()

        meta_rows = [
            ["Sell Report Date", report_date],
            ["Created By", rows[0].created_by or ""],
            ["Created At", rows[0].created_at],
        ]
        items_rows = [["Brand", "Size", "Sold(c)", "Sold(b)", "Amount"]]
        for r in rows:
            items_rows.append([
                r.brand_name or "",
                f"{r.pack_size_case}/{r.pack_size_quantity_ml}ml",
                r.sold_cases,
                r.sold_bottles,
                r.sell_amount
            ])

        finance_rows = []
        expense_rows = []
        if fin:
            finance_rows = [
                ["Total Sell Amount", fin.total_sell_amount],
                ["Last Balance Amount", fin.last_balance_amount],
                ["Total Amount", fin.total_amount],
                ["UPI/PhonePay", fin.upi_phonepay],
                ["Cash", fin.cash],
                ["Total Balance", fin.total_balance],
                ["Total Expenses", fin.total_expenses],
                ["Final Balance", fin.final_balance],
            ]
            if expenses:
                expense_rows = [["Expense", "Amount"]]
                for e in expenses:
                    expense_rows.append([e.name, e.amount])

        out_dir = os.path.join("requested_pdf", "sellreport")
        safe_date = str(report_date).replace("/", "-").replace("\\", "-")
        filename = f"sell_report_{safe_date}.pdf"
        out_path = os.path.join(out_dir, filename)
        write_sell_report_pdf(out_path, meta_rows, items_rows, finance_rows, expense_rows, title="Sell Report")
        return send_file(out_path, as_attachment=True, download_name=filename)
    finally:
        db.close()

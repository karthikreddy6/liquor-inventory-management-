from flask import Blueprint, jsonify, Response, send_file, request
from sqlalchemy import text, func
import time
import os
import base64
from functools import wraps
from reportlab.lib.units import mm
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
    SellFinancePhonePay,
    SellFinanceCash,
    SellFinanceOutsideIncome,
    PriceListItem,
    AuditLog,
    UserLogin,
)
from auth import jwt_required
from config import APP_START_TIME, ADMIN_USER, ADMIN_PASS
from services.pdf_export import (
    write_date_range_summary_pdf,
    write_invoice_pdf,
    write_present_stock_pdf,
    write_range_sections_pdf,
    write_sell_report_pdf,
)
from services.audit import log_action, update_last_login
from services.stock_service import recalc_stock_summary
from services.sales_utils import parse_report_date
from models import PriceListItem

admin_bp = Blueprint("admin", __name__)

def get_auth_from_header():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        return None, None
    
    if auth_header.startswith("Basic "):
        try:
            encoded = auth_header.split(" ", 1)[1]
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)
            return "basic", (username, password)
        except Exception:
            return "invalid", None
            
    if auth_header.startswith("Bearer "):
        return "jwt", auth_header.split(" ", 1)[1]
        
    return "unknown", None

def admin_or_staff_required(fn):
    """Allows either Basic Auth (Admin) or JWT (Owner/Supervisor)"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        mode, data = get_auth_from_header()
        
        if mode == "basic":
            username, password = data
            if username == ADMIN_USER and password == ADMIN_PASS:
                request.user = {"username": username, "role": "admin"}
                request.auth_mode = "basic"
                db = SessionLocal()
                try:
                    update_last_login(db, request.user)
                    log_action(db, request.user, "api_access", "admin_route", request.path)
                    db.commit()
                finally:
                    db.close()
                return fn(*args, **kwargs)
            return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="Admin"'})
            
        if mode == "jwt":
            request.auth_mode = "jwt"
            return jwt_required(roles=["owner", "supervisor"])(fn)(*args, **kwargs)
            
        return Response("Missing or invalid Authorization header", 401, {"WWW-Authenticate": 'Basic realm="Admin"'})
    return wrapper

def admin_basic_required(fn):
    """Strictly requires Basic Auth (Admin)"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        mode, data = get_auth_from_header()
        if mode == "basic":
            username, password = data
            if username == ADMIN_USER and password == ADMIN_PASS:
                request.user = {"username": username, "role": "admin"}
                request.auth_mode = "basic"
                db = SessionLocal()
                try:
                    update_last_login(db, request.user)
                    log_action(db, request.user, "admin_action", "management", request.path)
                    db.commit()
                finally:
                    db.close()
                return fn(*args, **kwargs)
        return Response("Unauthorized: Basic Auth Required", 401, {"WWW-Authenticate": 'Basic realm="Admin"'})
    return wrapper


def _in_date_range(raw_date, date_from, date_to):
    parsed = parse_report_date(raw_date)
    if not parsed:
        return False, None
    return date_from <= parsed <= date_to, parsed


def _parse_date_range_from_request():
    date_from_raw = str(request.args.get("date_from") or request.args.get("start_date") or "").strip()
    date_to_raw = str(request.args.get("date_to") or request.args.get("end_date") or "").strip()
    if not date_from_raw or not date_to_raw:
        return None, None, {"error": "date_from and date_to are required"}, 400

    date_from = parse_report_date(date_from_raw)
    date_to = parse_report_date(date_to_raw)
    if not date_from or not date_to:
        return None, None, {"error": "invalid date_from or date_to format"}, 400
    if date_from > date_to:
        return None, None, {"error": "date_from must be on or before date_to"}, 400
    return date_from, date_to, None, None


def _rebuild_stock_from_invoices(db):
    # Rebuild present stock entirely from remaining invoice items
    db.query(PresentStockDetail).delete()
    summary = db.query(StockSummary).first()
    if not summary:
        summary = StockSummary(total_cases_all_items=0, total_price_all_items=0.0)
        db.add(summary)
        db.flush()
    summary.total_cases_all_items = 0
    summary.total_price_all_items = 0.0
    summary.last_updated_item_name = ""

    mrp_map = {}
    for r in db.query(PriceListItem).all():
        key = (str(r.brand_number or "").strip(), str(r.pack_type or "").strip(), int(r.volume_ml or 0))
        if key not in mrp_map:
            mrp_map[key] = float(r.mrp or 0.0)

    invoices = db.query(Invoice).order_by(Invoice.id.asc()).all()
    invoice_date_map = {inv.invoice_number: inv.invoice_date for inv in invoices}

    items = db.query(InvoiceItem).order_by(InvoiceItem.id.asc()).all()
    stock_map = {}
    for it in items:
        key = (it.brand_number, it.pack_size_case, it.pack_size_quantity_ml)
        pack_size = int(it.pack_size_case or 0)
        cases = int(it.cases_delivered or 0)
        bottles = int(it.bottles_delivered or 0)
        total_bottles = cases * pack_size + bottles

        mrp_key = (str(it.brand_number or "").strip(), str(it.pack_type or "").strip(), int(it.pack_size_quantity_ml or 0))
        mrp = mrp_map.get(mrp_key)
        unit_rate = float(mrp) if mrp is not None else None
        rate_per_case = float(mrp) * float(pack_size) if (mrp is not None and pack_size) else None
        total_amount = float(mrp) * float(total_bottles) if mrp is not None else 0.0

        if key not in stock_map:
            stock_map[key] = {
                "brand_number": it.brand_number,
                "brand_name": it.brand_name,
                "product_type": it.product_type,
                "pack_type": it.pack_type,
                "pack_size_case": it.pack_size_case,
                "pack_size_quantity_ml": it.pack_size_quantity_ml,
                "total_cases": 0,
                "total_bottles": 0,
                "rate_per_case": rate_per_case,
                "unit_rate_per_bottle": unit_rate,
                "total_amount": 0.0,
                "last_invoice_date": invoice_date_map.get(it.invoice_number, ""),
            }

        entry = stock_map[key]
        entry["total_cases"] += cases
        entry["total_bottles"] += total_bottles
        entry["total_amount"] += total_amount
        entry["rate_per_case"] = rate_per_case or entry.get("rate_per_case")
        entry["unit_rate_per_bottle"] = unit_rate or entry.get("unit_rate_per_bottle")
        entry["last_invoice_date"] = invoice_date_map.get(it.invoice_number, entry["last_invoice_date"])

        item_display = f"{it.brand_name or ''} {it.pack_size_quantity_ml or 0}ml/{it.pack_size_case or 0}"
        entry["last_updated_item_name"] = item_display

    for entry in stock_map.values():
        db.add(PresentStockDetail(**entry))
        summary.total_cases_all_items += entry["total_cases"] or 0
        summary.total_price_all_items += entry["total_amount"] or 0.0
        summary.last_updated_item_name = entry.get("last_updated_item_name") or summary.last_updated_item_name

# --- Information Endpoints (Option 1 & 2) ---

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
        invoice_rows = db.query(Invoice).order_by(Invoice.id.desc()).limit(50).all()

        # Calculate MRP value
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
        invoice_count, item_count, stock_count, total_stock_mrp_value = 0, 0, 0, 0.0
        latest_invoice, summary = None, None
        invoice_rows = []
    finally:
        db.close()

    auth_mode = getattr(request, "auth_mode", "jwt")
    
    if request.headers.get("Accept") == "application/json":
        return jsonify({
            "status": "ok" if db_ok else "error",
            "auth_mode": auth_mode,
            "invoice_count": invoice_count,
            "item_count": item_count,
            "stock_count": stock_count,
            "latest_invoice_number": latest_invoice.invoice_number if latest_invoice else "N/A",
            "latest_invoice_date": latest_invoice.invoice_date if latest_invoice else "N/A",
            "total_stock_mrp_value": total_stock_mrp_value,
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "uptime_seconds": int(time.time() - APP_START_TIME)
        })
    auth_badge = "Basic Auth" if auth_mode == "basic" else "JWT Bearer"
    server_time = time.strftime("%Y-%m-%d %H:%M:%S")
    uptime_seconds = int(time.time() - APP_START_TIME)
    latest_invoice_date = latest_invoice.invoice_date if latest_invoice else "N/A"
    latest_invoice_number = latest_invoice.invoice_number if latest_invoice else "N/A"
    stock_summary_updated_at = (
        summary.updated_at.isoformat() if summary and summary.updated_at else "N/A"
    )

    rows_html = "".join([
        f"<tr>"
        f"<td style='padding:8px; border:1px solid #e5e7eb;'>{r.invoice_number}</td>"
        f"<td style='padding:8px; border:1px solid #e5e7eb;'>{r.invoice_date}</td>"
        f"<td style='padding:8px; border:1px solid #e5e7eb;'>{r.retailer_code}</td>"
        f"<td style='padding:8px; border:1px solid #e5e7eb;'>{r.uploaded_by or ''}</td>"
        f"<td style='padding:8px; border:1px solid #e5e7eb;'>{(r.uploaded_at.isoformat() if r.uploaded_at else (r.created_at.isoformat() if r.created_at else ''))}</td>"
        f"<td style='padding:8px; border:1px solid #e5e7eb;'>"
        f"<button data-invoice='{r.invoice_number}' class='del-btn' style='padding:4px 8px;'>Delete</button>"
        f"</td>"
        f"</tr>"
        for r in invoice_rows
    ])

    html = f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
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
      h1 {{
        margin: 0 0 6px 0;
        letter-spacing: 0.5px;
      }}
      .sub {{
        color: var(--muted);
        font-size: 14px;
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
    </style>
  </head>
  <body>
    <header>
      <h1>Admin Dashboard <span class="badge">{auth_badge}</span></h1>
      <div class="sub">Server time: {server_time} - Uptime: {uptime_seconds}s - DB: <span class="status">{("OK" if db_ok else "ERROR")}</span></div>
      {f'<div class="muted">DB error: {db_error}</div>' if db_error else ''}
    </header>
    <section class="grid">
      <div class="card">
        <div class="label">Invoices</div>
        <div class="value">{invoice_count}</div>
      </div>
      <div class="card">
        <div class="label">Invoice Items</div>
        <div class="value">{item_count}</div>
      </div>
      <div class="card">
        <div class="label">Stock Items</div>
        <div class="value">{stock_count}</div>
      </div>
      <div class="card">
        <div class="label">Latest Invoice</div>
        <div class="value">{latest_invoice_number}</div>
        <div class="muted">{latest_invoice_date}</div>
      </div>
      <div class="card">
        <div class="label">Stock Summary Updated</div>
        <div class="value">{stock_summary_updated_at}</div>
      </div>
      <div class="card">
        <div class="label">Total Stock Value (MRP)</div>
        <div class="value">{total_stock_mrp_value:.2f}</div>
      </div>
    </section>
    <section class="grid">
      <div class="card" style="grid-column: 1 / -1;">
        <div class="label">Uploaded Invoices (Latest 50)</div>
        <div class="muted">Delete is available only in Basic Auth mode.</div>
        <div style="overflow-x:auto; margin-top:10px;">
          <table style="width:100%; border-collapse: collapse; font-size: 13px;">
            <thead>
              <tr style="background:#f3f4f6;">
                <th style="text-align:left; padding:8px; border:1px solid #e5e7eb;">Invoice Number</th>
                <th style="text-align:left; padding:8px; border:1px solid #e5e7eb;">Invoice Date</th>
                <th style="text-align:left; padding:8px; border:1px solid #e5e7eb;">Retailer Code</th>
                <th style="text-align:left; padding:8px; border:1px solid #e5e7eb;">Uploaded By</th>
                <th style="text-align:left; padding:8px; border:1px solid #e5e7eb;">Uploaded At</th>
                <th style="text-align:left; padding:8px; border:1px solid #e5e7eb;">Action</th>
              </tr>
            </thead>
            <tbody>
              {rows_html}
            </tbody>
          </table>
        </div>
      </div>
    </section>
    <script>
      const authMode = "{auth_badge}";
      const buttons = document.querySelectorAll(".del-btn");
      if (authMode !== "Basic Auth") {{
        buttons.forEach(b => {{
          b.disabled = true;
          b.style.opacity = 0.5;
          b.title = "Delete requires Basic Auth";
        }});
      }}
      buttons.forEach(btn => {{
        btn.addEventListener("click", async () => {{
          if (authMode !== "Basic Auth") return;
          const invoice = btn.getAttribute("data-invoice");
          if (!confirm(`Delete invoice ${{invoice}}?`)) return;
          const user = prompt("Admin username:", "admin");
          const pass = prompt("Admin password:");
          if (!user || !pass) return;
          const token = btoa(`${{user}}:${{pass}}`);
          const res = await fetch(`/admin/invoices/${{invoice}}`, {{
            method: "DELETE",
            headers: {{ "Authorization": `Basic ${{token}}` }}
          }});
          if (res.ok) location.reload();
          else alert("Delete failed");
        }});
      }});
    </script>
  </body>
</html>
"""
    return Response(html, mimetype="text/html")

@admin_bp.route("/admin/status", methods=["GET"])
@admin_or_staff_required
def admin_status():
    return jsonify({
        "status": "ok",
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_seconds": int(time.time() - APP_START_TIME)
    })


@admin_bp.route("/dashboard/summary", methods=["GET"])
@admin_or_staff_required
def dashboard_summary():
    db = SessionLocal()
    try:
        last_finance = db.query(SellFinance).order_by(SellFinance.created_at.desc()).first()
        total_uncleared_balance = float(last_finance.final_balance or 0.0) if last_finance else 0.0

        last_invoice = db.query(Invoice).order_by(Invoice.id.desc()).first()
        last_invoice_date = last_invoice.invoice_date if last_invoice else ""
        last_invoice_number = last_invoice.invoice_number if last_invoice else ""

        last_invoice_value = 0.0
        last_invoice_brands_count = 0
        if last_invoice:
            totals = db.query(InvoiceTotals).filter(
                InvoiceTotals.invoice_number == last_invoice.invoice_number
            ).first()
            if totals:
                last_invoice_value = float(totals.total_invoice_value or 0.0)
            last_invoice_brands_count = db.query(
                func.count(func.distinct(InvoiceItem.brand_number))
            ).filter(InvoiceItem.invoice_number == last_invoice.invoice_number).scalar()
        last_invoice_brands_count = int(last_invoice_brands_count or 0)

        total_present_stock = db.query(
            func.coalesce(func.sum(PresentStockDetail.total_cases), 0)
        ).scalar()
        total_present_stock = float(total_present_stock or 0)

        summary = db.query(StockSummary).first()
        total_present_stock_mrp_value = float(summary.total_price_all_items or 0.0) if summary else 0.0

        last_report = db.query(SellReport).order_by(SellReport.created_at.desc()).first()
        last_report_date = last_report.report_date if last_report else ""
        last_sell_report_value = 0.0
        if last_report_date:
            last_sell_report_value = db.query(
                func.coalesce(func.sum(SellReport.sell_amount), 0.0)
            ).filter(SellReport.report_date == last_report_date).scalar()

        total_sell_mrp = db.query(
            func.coalesce(func.sum(SellReport.sell_amount), 0.0)
        ).scalar()
        total_invoices_value = db.query(
            func.coalesce(func.sum(InvoiceTotals.total_invoice_value), 0.0)
        ).scalar()

        return jsonify({
            "total_present_stock": total_present_stock,
            "total_present_stock_mrp_value": total_present_stock_mrp_value,
            "last_sell_report_date": last_report_date,
            "last_sell_report_value": float(last_sell_report_value or 0.0),
            "last_invoice_date": last_invoice_date,
            "last_invoice_value": last_invoice_value,
            "last_invoice_number": last_invoice_number,
            "last_invoice_brands_count": last_invoice_brands_count,
            "total_sell_mrp": float(total_sell_mrp or 0.0),
            "total_invoices_value": float(total_invoices_value or 0.0),
            "total_uncleared_balance": total_uncleared_balance,
        })
    finally:
        db.close()

# --- System Logs (Option 2 ONLY) ---

@admin_bp.route("/admin/audit-logs", methods=["GET"])
@admin_basic_required
def get_audit_logs():
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(200).all()
        return jsonify({
            "count": len(rows),
            "items": [{
                "username": r.username,
                "role": r.role,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "details": r.details,
                "created_at": r.created_at.isoformat() if r.created_at else None
            } for r in rows]
        })
    finally:
        db.close()

@admin_bp.route("/admin/user-logins", methods=["GET"])
@admin_basic_required
def get_user_logins():
    db = SessionLocal()
    try:
        rows = db.query(UserLogin).order_by(UserLogin.last_login_at.desc()).all()
        return jsonify({
            "count": len(rows),
            "items": [{
                "username": r.username,
                "role": r.role,
                "last_login_at": r.last_login_at.isoformat() if r.last_login_at else None
            } for r in rows]
        })
    finally:
        db.close()

# --- Management Actions (Option 2 ONLY) ---

@admin_bp.route("/admin/reports/sell-reports/<report_date>", methods=["DELETE"])
@admin_basic_required
def delete_sell_report(report_date):
    db = SessionLocal()
    try:
        rows = db.query(SellReport).filter(SellReport.report_date == report_date).all()
        if not rows:
            return {"error": "sell report not found"}, 404
        for r in rows:
            db.delete(r)

        fin = db.query(SellFinance).filter(SellFinance.report_date == report_date).first()
        if fin:
            db.query(SellFinanceExpense).filter(SellFinanceExpense.finance_id == fin.id).delete()
            db.query(SellFinanceOutsideIncome).filter(SellFinanceOutsideIncome.finance_id == fin.id).delete()
            db.query(SellFinancePhonePay).filter(SellFinancePhonePay.finance_id == fin.id).delete()
            db.query(SellFinanceCash).filter(SellFinanceCash.finance_id == fin.id).delete()
            db.delete(fin)
        log_action(db, request.user, "DELETE_SELL_REPORT", "sell_report", report_date)
        _rebuild_stock_from_invoices(db)
        db.commit()
        return jsonify({"status": "ok", "message": f"Deleted report for {report_date}"})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()

@admin_bp.route("/admin/invoices/<invoice_number>", methods=["DELETE"])
@admin_basic_required
def delete_invoice(invoice_number):
    db = SessionLocal()
    try:
        invoice_exists = db.query(Invoice).filter(Invoice.invoice_number == invoice_number).first()
        if not invoice_exists:
            return {"error": "invoice not found"}, 404

        db.query(InvoiceItem).filter(InvoiceItem.invoice_number == invoice_number).delete()
        db.query(InvoiceTotals).filter(InvoiceTotals.invoice_number == invoice_number).delete()
        db.query(Invoice).filter(Invoice.invoice_number == invoice_number).delete()
        log_action(db, request.user, "DELETE_INVOICE", "invoice", invoice_number)
        _rebuild_stock_from_invoices(db)
        db.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()

@admin_bp.route("/admin/sell-finance/<report_date>", methods=["DELETE"])
@admin_basic_required
def delete_sell_finance(report_date):
    db = SessionLocal()
    try:
        fin = db.query(SellFinance).filter(SellFinance.report_date == report_date).first()
        if not fin: return {"error": "not found"}, 404
        db.query(SellFinanceExpense).filter(SellFinanceExpense.finance_id == fin.id).delete()
        db.query(SellFinanceOutsideIncome).filter(SellFinanceOutsideIncome.finance_id == fin.id).delete()
        db.query(SellFinancePhonePay).filter(SellFinancePhonePay.finance_id == fin.id).delete()
        db.query(SellFinanceCash).filter(SellFinanceCash.finance_id == fin.id).delete()
        db.delete(fin)
        log_action(db, request.user, "DELETE_FINANCE", "sell_finance", report_date)
        db.commit()
        return jsonify({"status": "ok"})
    finally:
        db.close()

@admin_bp.route("/admin/stock/<int:stock_id>", methods=["PATCH"])
@admin_basic_required
def update_stock(stock_id):
    payload = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        stock = db.query(PresentStockDetail).filter(PresentStockDetail.id == stock_id).first()
        if not stock: return {"error": "stock not found"}, 404
        for f in ["total_cases", "total_bottles", "rate_per_case", "unit_rate_per_bottle", "total_amount"]:
            if f in payload: setattr(stock, f, payload.get(f))
        log_action(db, request.user, "EDIT_STOCK", "stock", stock_id, details=str(payload))
        db.commit()
        return jsonify({"status": "ok"})
    finally:
        db.close()

# --- Standard Report List Endpoints (Option 1 & 2) ---

@admin_bp.route("/reports/invoices", methods=["GET"])
@admin_or_staff_required
def list_invoices():
    db = SessionLocal()
    try:
        rows = db.query(Invoice).order_by(Invoice.id.desc()).all()
        return jsonify([{
            "invoice_number": r.invoice_number,
            "invoice_date": r.invoice_date,
            "uploaded_by": r.uploaded_by or "unknown",
            "uploaded_at": (r.uploaded_at.isoformat() if r.uploaded_at else (r.created_at.isoformat() if r.created_at else None)),
            "retailer_code": r.retailer_code
        } for r in rows])
    finally:
        db.close()

@admin_bp.route("/reports/sell-reports", methods=["GET"])
@admin_or_staff_required
def list_sell_reports():
    db = SessionLocal()
    try:
        rows = db.query(SellReport).order_by(SellReport.created_at.desc()).all()
        finances = {f.report_date: f for f in db.query(SellFinance).all()}
        summary = {}
        for r in rows:
            key = r.report_date
            if key not in summary:
                fin = finances.get(key)
                summary[key] = {
                    "report_date": r.report_date,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "created_by": r.created_by or "unknown",
                    "total_items": 0,
                    "edit_count": r.edit_count or 0,
                    "finance": {
                        "total_sell_amount": fin.total_sell_amount,
                        "total_balance": fin.total_balance,
                        "final_balance": fin.final_balance
                    } if fin else None
                }
            summary[key]["total_items"] += 1
        return jsonify(list(summary.values()))
    finally:
        db.close()

@admin_bp.route("/admin/invoices/<invoice_number>", methods=["PATCH"])
@admin_basic_required
def admin_update_invoice(invoice_number):
    payload = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number).first()
        if not invoice: return {"error": "invoice not found"}, 404
        
        if "invoice_date" in payload: invoice.invoice_date = payload.get("invoice_date")
        if "invoice_number" in payload: invoice.invoice_number = payload.get("invoice_number")
        
        log_action(db, request.user, "EDIT_INVOICE", "invoice", invoice_number, details=str(payload))
        db.commit()
        return jsonify({"status": "ok"})
    finally:
        db.close()


@admin_bp.route("/reports/summary/pdf", methods=["GET"])
@admin_or_staff_required
def date_range_summary_pdf():
    date_from, date_to, err, status = _parse_date_range_from_request()
    if err:
        return err, status

    db = SessionLocal()
    try:
        invoice_rows_all = db.query(Invoice).all()
        invoices_in_range = []
        for invoice in invoice_rows_all:
            allowed, parsed_date = _in_date_range(invoice.invoice_date, date_from, date_to)
            if allowed:
                invoices_in_range.append((parsed_date, invoice))
        invoices_in_range.sort(key=lambda row: (row[0], row[1].invoice_number or ""))

        invoice_numbers = [row[1].invoice_number for row in invoices_in_range if row[1].invoice_number]
        invoice_totals_map = {}
        if invoice_numbers:
            for total in db.query(InvoiceTotals).filter(InvoiceTotals.invoice_number.in_(invoice_numbers)).all():
                invoice_totals_map[total.invoice_number] = total

        sell_rows_all = db.query(SellReport).all()
        sell_summary_map = {}
        for row in sell_rows_all:
            allowed, parsed_date = _in_date_range(row.report_date, date_from, date_to)
            if not allowed:
                continue
            bucket = sell_summary_map.setdefault(row.report_date, {
                "parsed_date": parsed_date,
                "total_items": 0,
                "total_sell_amount": 0.0,
            })
            bucket["total_items"] += 1
            bucket["total_sell_amount"] += float(row.sell_amount or 0.0)

        finance_rows_all = db.query(SellFinance).all()
        finances_in_range = []
        finance_by_report_date = {}
        for finance in finance_rows_all:
            allowed, parsed_date = _in_date_range(finance.report_date, date_from, date_to)
            if not allowed:
                continue
            finance_by_report_date[finance.report_date] = finance
            finances_in_range.append((parsed_date, finance))
        finances_in_range.sort(key=lambda row: (row[0], row[1].report_date or ""))

        finance_ids = [row[1].id for row in finances_in_range if getattr(row[1], "id", None)]
        expense_totals_map = {}
        outside_income_totals_map = {}
        if finance_ids:
            for row in db.query(SellFinanceExpense).filter(SellFinanceExpense.finance_id.in_(finance_ids)).all():
                expense_totals_map[row.finance_id] = expense_totals_map.get(row.finance_id, 0.0) + float(row.amount or 0.0)
            for row in db.query(SellFinanceOutsideIncome).filter(SellFinanceOutsideIncome.finance_id.in_(finance_ids)).all():
                outside_income_totals_map[row.finance_id] = outside_income_totals_map.get(row.finance_id, 0.0) + float(row.amount or 0.0)

        if not invoices_in_range and not sell_summary_map and not finances_in_range:
            return {"error": "no data found in selected date range"}, 404

        invoice_count = len(invoices_in_range)
        total_invoice_value = 0.0
        total_net_invoice_value = 0.0
        invoice_table_rows = []
        for parsed_date, invoice in invoices_in_range:
            totals = invoice_totals_map.get(invoice.invoice_number)
            net_value = float(totals.net_invoice_value or 0.0) if totals else 0.0
            invoice_value = float(totals.total_invoice_value or 0.0) if totals else 0.0
            total_net_invoice_value += net_value
            total_invoice_value += invoice_value
            invoice_table_rows.append([
                parsed_date.strftime("%Y-%m-%d"),
                invoice.invoice_number,
                net_value,
                invoice_value,
            ])

        sell_summary_rows = []
        total_sell_amount = 0.0
        total_sell_items = 0
        for report_date, bucket in sorted(sell_summary_map.items(), key=lambda row: (row[1]["parsed_date"], row[0])):
            finance = finance_by_report_date.get(report_date)
            total_sell_amount += float(bucket["total_sell_amount"] or 0.0)
            total_sell_items += int(bucket["total_items"] or 0)
            sell_summary_rows.append([
                bucket["parsed_date"].strftime("%Y-%m-%d"),
                int(bucket["total_items"] or 0),
                float(bucket["total_sell_amount"] or 0.0),
                float(finance.final_balance or 0.0) if finance else 0.0,
            ])

        finance_table_rows = []
        total_expenses = 0.0
        total_outside_income = 0.0
        latest_final_balance = 0.0
        if finances_in_range:
            latest_final_balance = float(finances_in_range[-1][1].final_balance or 0.0)
        for parsed_date, finance in finances_in_range:
            expense_total = float(expense_totals_map.get(finance.id, 0.0))
            outside_income_total = float(outside_income_totals_map.get(finance.id, 0.0))
            total_expenses += expense_total
            total_outside_income += outside_income_total
            finance_table_rows.append([
                parsed_date.strftime("%Y-%m-%d"),
                float(finance.total_sell_amount or 0.0),
                expense_total,
                outside_income_total,
                float(finance.final_balance or 0.0),
            ])

        overview_rows = [
            ["Invoices Count", invoice_count],
            ["Total Invoice Value", total_invoice_value],
            ["Total Net Invoice Value", total_net_invoice_value],
            ["Sell Report Days", len(sell_summary_rows)],
            ["Sell Report Items", total_sell_items],
            ["Total Sell Amount", total_sell_amount],
            ["Finance Records", len(finances_in_range)],
            ["Total Expenses", total_expenses],
            ["Total Outside Income", total_outside_income],
            ["Latest Final Balance", latest_final_balance],
        ]
        generated_by = ((request.user or {}).get("username") or "").strip() or "Nagarjun"
        meta_rows = [
            ["date_from", date_from.strftime("%Y-%m-%d")],
            ["date_to", date_to.strftime("%Y-%m-%d")],
            ["generated_by", generated_by],
            ["generated_at", time.strftime("%Y-%m-%d %H:%M:%S")],
        ]

        out_dir = os.path.join("requested_pdf", "summary")
        os.makedirs(out_dir, exist_ok=True)
        filename = f"summary_{date_from.strftime('%Y-%m-%d')}_to_{date_to.strftime('%Y-%m-%d')}.pdf"
        out_path = os.path.join(out_dir, filename)
        write_date_range_summary_pdf(
            out_path,
            meta_rows,
            overview_rows,
            invoice_table_rows,
            sell_summary_rows,
            finance_table_rows,
            title="Monthly Summary",
        )
        return send_file(out_path, as_attachment=True, download_name=filename)
    finally:
        db.close()


@admin_bp.route("/reports/invoices/pdf", methods=["GET"])
@admin_or_staff_required
def invoice_date_range_pdf():
    date_from, date_to, err, status = _parse_date_range_from_request()
    if err:
        return err, status

    db = SessionLocal()
    try:
        invoice_rows_all = db.query(Invoice).all()
        invoices_in_range = []
        for invoice in invoice_rows_all:
            allowed, parsed_date = _in_date_range(invoice.invoice_date, date_from, date_to)
            if allowed:
                invoices_in_range.append((parsed_date, invoice))
        invoices_in_range.sort(key=lambda row: (row[0], row[1].invoice_number or ""))
        if not invoices_in_range:
            return {"error": "no invoices found in selected date range"}, 404

        invoice_numbers = [row[1].invoice_number for row in invoices_in_range if row[1].invoice_number]
        invoice_totals_map = {}
        if invoice_numbers:
            for total in db.query(InvoiceTotals).filter(InvoiceTotals.invoice_number.in_(invoice_numbers)).all():
                invoice_totals_map[total.invoice_number] = total
        invoice_items = []
        if invoice_numbers:
            invoice_items = db.query(InvoiceItem).filter(
                InvoiceItem.invoice_number.in_(invoice_numbers)
            ).order_by(
                InvoiceItem.invoice_number.asc(),
                InvoiceItem.sl_no.asc(),
                InvoiceItem.id.asc(),
            ).all()

        total_invoice_value = 0.0
        total_net_invoice_value = 0.0
        total_credit_balance = 0.0
        invoice_table_rows = []
        for parsed_date, invoice in invoices_in_range:
            totals = invoice_totals_map.get(invoice.invoice_number)
            net_value = float(totals.net_invoice_value or 0.0) if totals else 0.0
            invoice_value = float(totals.total_invoice_value or 0.0) if totals else 0.0
            credit_balance = float(totals.retailer_credit_balance or 0.0) if totals else 0.0
            total_net_invoice_value += net_value
            total_invoice_value += invoice_value
            total_credit_balance += credit_balance
            invoice_table_rows.append([
                parsed_date.strftime("%Y-%m-%d"),
                invoice.invoice_number,
                invoice.retailer_code or "",
                invoice.uploaded_by or "",
                net_value,
                invoice_value,
                credit_balance,
            ])
        invoice_item_rows = []
        invoice_date_map = {
            invoice.invoice_number: parsed_date.strftime("%Y-%m-%d")
            for parsed_date, invoice in invoices_in_range
        }
        for item in invoice_items:
            invoice_item_rows.append([
                invoice_date_map.get(item.invoice_number, ""),
                item.invoice_number,
                item.brand_name or "",
                f"{item.pack_size_case or 0}/{item.pack_size_quantity_ml or 0}ml",
                int(item.cases_delivered or 0),
                int(item.bottles_delivered or 0),
                float(item.total_amount or 0.0),
            ])

        meta_rows = [
            ["date_from", date_from.strftime("%Y-%m-%d")],
            ["date_to", date_to.strftime("%Y-%m-%d")],
            ["generated_by", ((request.user or {}).get("username") or "").strip() or "Nagarjun"],
            ["generated_at", time.strftime("%Y-%m-%d %H:%M:%S")],
        ]
        overview_rows = [
            ["Invoices Count", len(invoices_in_range)],
            ["Total Net Invoice Value", total_net_invoice_value],
            ["Total Invoice Value", total_invoice_value],
            ["Total Retailer Credit Balance", total_credit_balance],
        ]
        sections = [
            {
                "title": "Daily Invoices",
                "headers": ["Invoice Date", "Invoice Number", "Retailer Code", "Uploaded By", "Net Value", "Total Value", "Credit Balance"],
                "rows": invoice_table_rows,
                "col_widths": [24 * mm, 34 * mm, 24 * mm, 24 * mm, 24 * mm, 24 * mm, 26 * mm],
            },
            {
                "title": "Daily Invoice Item Details",
                "headers": ["Invoice Date", "Invoice Number", "Brand", "Size", "Cases", "Bottles", "Amount"],
                "rows": invoice_item_rows,
                "col_widths": [22 * mm, 30 * mm, 68 * mm, 18 * mm, 16 * mm, 16 * mm, 20 * mm],
            }
        ]

        out_dir = os.path.join("requested_pdf", "invoices")
        os.makedirs(out_dir, exist_ok=True)
        filename = f"invoices_{date_from.strftime('%Y-%m-%d')}_to_{date_to.strftime('%Y-%m-%d')}.pdf"
        out_path = os.path.join(out_dir, filename)
        write_range_sections_pdf(out_path, meta_rows, overview_rows, sections, title="Invoices Date Range")
        return send_file(out_path, as_attachment=True, download_name=filename)
    finally:
        db.close()


@admin_bp.route("/reports/sell-reports/pdf", methods=["GET"])
@admin_or_staff_required
def sell_report_date_range_pdf():
    date_from, date_to, err, status = _parse_date_range_from_request()
    if err:
        return err, status

    db = SessionLocal()
    try:
        sell_rows_all = db.query(SellReport).order_by(
            SellReport.report_date.asc(),
            SellReport.created_at.asc(),
            SellReport.id.asc(),
        ).all()
        finance_rows_all = db.query(SellFinance).all()
        finance_by_report_date = {row.report_date: row for row in finance_rows_all if row.report_date}

        finance_ids = [row.id for row in finance_rows_all if getattr(row, "id", None)]
        expense_totals_map = {}
        outside_income_totals_map = {}
        if finance_ids:
            for row in db.query(SellFinanceExpense).filter(SellFinanceExpense.finance_id.in_(finance_ids)).all():
                expense_totals_map[row.finance_id] = expense_totals_map.get(row.finance_id, 0.0) + float(row.amount or 0.0)
            for row in db.query(SellFinanceOutsideIncome).filter(SellFinanceOutsideIncome.finance_id.in_(finance_ids)).all():
                outside_income_totals_map[row.finance_id] = outside_income_totals_map.get(row.finance_id, 0.0) + float(row.amount or 0.0)

        daily_summary = {}
        sell_item_rows = []
        for row in sell_rows_all:
            allowed, parsed_date = _in_date_range(row.report_date, date_from, date_to)
            if not allowed:
                continue
            bucket = daily_summary.setdefault(row.report_date, {
                "parsed_date": parsed_date,
                "total_items": 0,
                "total_sell_amount": 0.0,
            })
            bucket["total_items"] += 1
            bucket["total_sell_amount"] += float(row.sell_amount or 0.0)
            sell_item_rows.append([
                parsed_date.strftime("%Y-%m-%d"),
                row.brand_name or "",
                f"{row.pack_size_case or 0}/{row.pack_size_quantity_ml or 0}ml",
                int(row.sold_cases or 0),
                int(row.sold_bottles or 0),
                float(row.sell_amount or 0.0),
            ])

        finance_range_rows = []
        for finance in finance_rows_all:
            allowed, parsed_date = _in_date_range(finance.report_date, date_from, date_to)
            if not allowed:
                continue
            finance_range_rows.append((parsed_date, finance))
        finance_range_rows.sort(key=lambda row: (row[0], row[1].report_date or ""))

        if not daily_summary and not finance_range_rows:
            return {"error": "no sell reports found in selected date range"}, 404

        total_sell_amount = 0.0
        total_sell_items = 0
        latest_final_balance = 0.0
        daily_rows = []
        for report_date, bucket in sorted(daily_summary.items(), key=lambda row: (row[1]["parsed_date"], row[0])):
            finance = finance_by_report_date.get(report_date)
            total_sell_amount += float(bucket["total_sell_amount"] or 0.0)
            total_sell_items += int(bucket["total_items"] or 0)
            final_balance = float(finance.final_balance or 0.0) if finance else 0.0
            latest_final_balance = final_balance or latest_final_balance
            daily_rows.append([
                bucket["parsed_date"].strftime("%Y-%m-%d"),
                int(bucket["total_items"] or 0),
                float(bucket["total_sell_amount"] or 0.0),
                final_balance,
            ])

        finance_daily_rows = []
        total_expenses = 0.0
        total_outside_income = 0.0
        for parsed_date, finance in finance_range_rows:
            expense_total = float(expense_totals_map.get(finance.id, 0.0))
            outside_income_total = float(outside_income_totals_map.get(finance.id, 0.0))
            total_expenses += expense_total
            total_outside_income += outside_income_total
            latest_final_balance = float(finance.final_balance or 0.0)
            finance_daily_rows.append([
                parsed_date.strftime("%Y-%m-%d"),
                float(finance.total_sell_amount or 0.0),
                expense_total,
                outside_income_total,
                float(finance.final_balance or 0.0),
            ])

        meta_rows = [
            ["date_from", date_from.strftime("%Y-%m-%d")],
            ["date_to", date_to.strftime("%Y-%m-%d")],
            ["generated_by", ((request.user or {}).get("username") or "").strip() or "Nagarjun"],
            ["generated_at", time.strftime("%Y-%m-%d %H:%M:%S")],
        ]
        overview_rows = [
            ["Sell Report Days", len(daily_rows)],
            ["Sell Report Items", total_sell_items],
            ["Total Sell Amount", total_sell_amount],
            ["Finance Records", len(finance_range_rows)],
            ["Total Expenses", total_expenses],
            ["Total Outside Income", total_outside_income],
            ["Latest Final Balance", latest_final_balance],
        ]
        sections = [
            {
                "title": "Daily Sell Reports",
                "headers": ["Report Date", "Items", "Sell Amount", "Final Balance"],
                "rows": daily_rows,
                "col_widths": [38 * mm, 28 * mm, 50 * mm, 50 * mm],
            },
            {
                "title": "Daily Sell Report Item Details",
                "headers": ["Report Date", "Brand", "Size", "Sold(C)", "Sold(B)", "Amount"],
                "rows": sell_item_rows,
                "col_widths": [24 * mm, 66 * mm, 22 * mm, 18 * mm, 18 * mm, 28 * mm],
            },
            {
                "title": "Daily Finance",
                "headers": ["Report Date", "Sell Amount", "Expenses", "Outside Income", "Final Balance"],
                "rows": finance_daily_rows,
                "col_widths": [28 * mm, 34 * mm, 34 * mm, 40 * mm, 40 * mm],
            },
        ]

        out_dir = os.path.join("requested_pdf", "sellreport")
        os.makedirs(out_dir, exist_ok=True)
        filename = f"sell_reports_{date_from.strftime('%Y-%m-%d')}_to_{date_to.strftime('%Y-%m-%d')}.pdf"
        out_path = os.path.join(out_dir, filename)
        write_range_sections_pdf(out_path, meta_rows, overview_rows, sections, title="Sell Reports Date Range")
        return send_file(out_path, as_attachment=True, download_name=filename)
    finally:
        db.close()

@admin_bp.route("/reports/invoices/<invoice_number>/pdf", methods=["GET"])
@admin_or_staff_required
def invoice_pdf(invoice_number):
    db = SessionLocal()
    try:
        invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number).first()
        if not invoice: return {"error": "not found"}, 404
        totals = db.query(InvoiceTotals).filter(InvoiceTotals.invoice_number == invoice_number).first()
        items = db.query(InvoiceItem).filter(InvoiceItem.invoice_number == invoice_number).order_by(InvoiceItem.sl_no.asc()).all()
        meta_rows = {
            "invoice_meta": {
                "invoice_number": invoice.invoice_number,
                "invoice_date": invoice.invoice_date,
            },
            "retailer": {
                "name": invoice.retailer_name,
                "code": invoice.retailer_code,
            },
            "licensee": {
                "pan": invoice.licensee_pan,
            },
        }
        totals_rows = {
            "e_challan_amount": totals.e_challan_amount if totals else 0,
            "previous_credit": totals.previous_credit if totals else 0,
            "sub_total": totals.sub_total if totals else 0,
            "special_excise_cess": totals.special_excise_cess if totals else 0,
            "tcs": totals.tcs if totals else 0,
            "new_retailer_professional_tax": totals.new_retailer_professional_tax if totals else 0,
            "retail_shop_excise_turnover_tax": totals.retail_shop_excise_turnover_tax if totals else 0,
            "less_this_invoice_value": totals.less_this_invoice_value if totals else 0,
            "retailer_credit_balance": totals.retailer_credit_balance if totals else 0,
            "invoice_value": totals.invoice_value if totals else 0,
            "mrp_round_off": totals.mrp_round_off if totals else 0,
            "net_invoice_value": totals.net_invoice_value if totals else 0,
            "total_invoice_value": totals.total_invoice_value if totals else 0,
        }
        items_rows = []
        for it in items:
            items_rows.append({
                "sl_no": it.sl_no,
                "brand_number": it.brand_number,
                "brand_name": it.brand_name,
                "product_type": it.product_type,
                "pack_type": it.pack_type,
                "pack_size_case": it.pack_size_case,
                "pack_size_quantity_ml": it.pack_size_quantity_ml,
                "cases_delivered": it.cases_delivered,
                "bottles_delivered": it.bottles_delivered,
                "rate_per_case": it.rate_per_case,
                "unit_rate_per_bottle": it.unit_rate_per_bottle,
                "total_amount": it.total_amount,
            })
        out_dir = os.path.join("requested_pdf", "invoices")
        os.makedirs(out_dir, exist_ok=True)
        filename = f"{invoice.invoice_number}.pdf"
        out_path = os.path.join(out_dir, filename)
        write_invoice_pdf(out_path, meta_rows, items_rows, totals_rows, title="Invoice Report")
        return send_file(out_path, as_attachment=True, download_name=filename)
    finally:
        db.close()

@admin_bp.route("/reports/sell-reports/<report_date>/pdf", methods=["GET"])
@admin_or_staff_required
def sell_report_pdf(report_date):
    db = SessionLocal()
    try:
        rows = db.query(SellReport).filter(SellReport.report_date == report_date).all()
        if not rows: return {"error": "not found"}, 404
        fin = db.query(SellFinance).filter(SellFinance.report_date == report_date).first()
        generated_by = ((request.user or {}).get("username") or "").strip() or "Nagarjun"
        created_by = ""
        if fin and getattr(fin, "created_by", None):
            created_by = str(fin.created_by).strip()
        if not created_by:
            created_by = str(rows[0].created_by or "").strip()
        total_sell_amount = float(fin.total_sell_amount or 0.0) if fin else float(sum(float(r.sell_amount or 0.0) for r in rows))
        final_balance = float(fin.final_balance or 0.0) if fin else 0.0
        finance_sections = []
        if fin and getattr(fin, "id", None):
            phonepay_rows = db.query(SellFinancePhonePay).filter(
                SellFinancePhonePay.finance_id == fin.id
            ).order_by(SellFinancePhonePay.txn_date.asc(), SellFinancePhonePay.id.asc()).all()
            cash_rows = db.query(SellFinanceCash).filter(
                SellFinanceCash.finance_id == fin.id
            ).order_by(SellFinanceCash.txn_date.asc(), SellFinanceCash.id.asc()).all()
            outside_income_rows = db.query(SellFinanceOutsideIncome).filter(
                SellFinanceOutsideIncome.finance_id == fin.id
            ).order_by(SellFinanceOutsideIncome.id.asc()).all()
            expense_rows = db.query(SellFinanceExpense).filter(
                SellFinanceExpense.finance_id == fin.id
            ).order_by(SellFinanceExpense.id.asc()).all()

            finance_sections = [
                {
                    "title": "PhonePe (UPI) By Date",
                    "headers": ["Date", "Amount (Rs.)"],
                    "rows": [[str(r.txn_date or ""), float(r.amount or 0.0)] for r in phonepay_rows],
                },
                {
                    "title": "Cash Collected By Date",
                    "headers": ["Date", "Amount (Rs.)"],
                    "rows": [[str(r.txn_date or ""), float(r.amount or 0.0)] for r in cash_rows],
                },
                {
                    "title": "Outside Income",
                    "headers": ["Name", "Amount (Rs.)"],
                    "rows": [[str(r.name or ""), float(r.amount or 0.0)] for r in outside_income_rows],
                },
                {
                    "title": "Outbound Expenses",
                    "headers": ["Name", "Amount (Rs.)"],
                    "rows": [[str(r.name or ""), float(r.amount or 0.0)] for r in expense_rows],
                },
            ]
        meta_rows = [
            ["report_date", report_date],
            ["generated_by", generated_by],
            ["created_by", created_by],
        ]
        items_rows = [["Brand", "Size", "Sold(c)", "Sold(b)", "Amount"]]
        for r in rows:
            items_rows.append([r.brand_name, f"{r.pack_size_case}/{r.pack_size_quantity_ml}ml", r.sold_cases, r.sold_bottles, r.sell_amount])
        finance_rows = [
            ["total_sell", total_sell_amount],
            ["final_balance", final_balance],
        ]
        out_dir = os.path.join("requested_pdf", "sellreport")
        os.makedirs(out_dir, exist_ok=True)
        safe_date = str(report_date).replace("/", "-")
        filename = f"sell_report_{safe_date}.pdf"
        out_path = os.path.join(out_dir, filename)
        write_sell_report_pdf(out_path, meta_rows, items_rows, finance_rows, finance_sections, title="Sell Report")
        return send_file(out_path, as_attachment=True, download_name=filename)
    finally:
        db.close()

@admin_bp.route("/reports/stock/pdf", methods=["GET"])
@admin_or_staff_required
def present_stock_pdf():
    db = SessionLocal()
    try:
        rows = db.query(PresentStockDetail).order_by(PresentStockDetail.brand_name.asc()).all()
        if not rows:
            return {"error": "no stock data"}, 404
        summary = db.query(StockSummary).first()
        total_bottles_all_items = sum(((r.total_cases or 0) * (r.pack_size_case or 0)) + (r.total_bottles or 0) for r in rows)

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

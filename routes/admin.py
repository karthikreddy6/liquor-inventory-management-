from flask import Blueprint, jsonify, Response, send_file, request
from sqlalchemy import text, func
import time
import os
import base64
from functools import wraps
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
from services.audit import log_action, update_last_login

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
          if (!confirm(`Delete invoice ${invoice}?`)) return;
          const user = prompt("Admin username:", "admin");
          const pass = prompt("Admin password:");
          if (!user || !pass) return;
          const token = btoa(`${{user}}:${{pass}}`);
          const res = await fetch(`/admin/invoices/${invoice}`, {{
            method: "DELETE",
            headers: {{ "Authorization": `Basic ${token}` }}
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
        db.query(SellReport).filter(SellReport.report_date == report_date).delete()
        fin = db.query(SellFinance).filter(SellFinance.report_date == report_date).first()
        if fin:
            db.query(SellFinanceExpense).filter(SellFinanceExpense.finance_id == fin.id).delete()
            db.delete(fin)
        log_action(db, request.user, "DELETE_SELL_REPORT", "sell_report", report_date)
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
        db.query(InvoiceItem).filter(InvoiceItem.invoice_number == invoice_number).delete()
        db.query(InvoiceTotals).filter(InvoiceTotals.invoice_number == invoice_number).delete()
        db.query(Invoice).filter(Invoice.invoice_number == invoice_number).delete()
        log_action(db, request.user, "DELETE_INVOICE", "invoice", invoice_number)
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

@admin_bp.route("/reports/invoices/<invoice_number>/pdf", methods=["GET"])
@admin_or_staff_required
def invoice_pdf(invoice_number):
    db = SessionLocal()
    try:
        invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number).first()
        if not invoice: return {"error": "not found"}, 404
        totals = db.query(InvoiceTotals).filter(InvoiceTotals.invoice_number == invoice_number).first()
        items = db.query(InvoiceItem).filter(InvoiceItem.invoice_number == invoice_number).order_by(InvoiceItem.sl_no.asc()).all()
        meta_rows = [["Invoice Number", invoice.invoice_number], ["Invoice Date", invoice.invoice_date], ["Retailer", f"{invoice.retailer_name} ({invoice.retailer_code})"]]
        totals_rows = [[k, v] for k, v in [["Value", totals.invoice_value], ["Net", totals.net_invoice_value]]] if totals else []
        items_rows = [["#", "Brand", "Pack", "Cases", "Bottles", "Total"]]
        for it in items:
            items_rows.append([it.sl_no, it.brand_name, f"{it.pack_size_case}/{it.pack_size_quantity_ml}ml", it.cases_delivered, it.bottles_delivered, it.total_amount])
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
        meta_rows = [["Sell Report Date", report_date], ["Created By", rows[0].created_by]]
        items_rows = [["Brand", "Size", "Sold(c)", "Sold(b)", "Amount"]]
        for r in rows:
            items_rows.append([r.brand_name, f"{r.pack_size_case}/{r.pack_size_quantity_ml}ml", r.sold_cases, r.sold_bottles, r.sell_amount])
        finance_rows = [[k, v] for k, v in [["Total Sell", fin.total_sell_amount], ["Final Balance", fin.final_balance]]] if fin else []
        out_dir = os.path.join("requested_pdf", "sellreport")
        os.makedirs(out_dir, exist_ok=True)
        safe_date = str(report_date).replace("/", "-")
        filename = f"sell_report_{safe_date}.pdf"
        out_path = os.path.join(out_dir, filename)
        write_sell_report_pdf(out_path, meta_rows, items_rows, finance_rows, [], title="Sell Report")
        return send_file(out_path, as_attachment=True, download_name=filename)
    finally:
        db.close()

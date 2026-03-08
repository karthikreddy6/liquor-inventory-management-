import json
import sqlite3
from pathlib import Path

JSON_PATH = "old_stock_from_db.json"
DB_PATH = "inventory.db"
REPLACE_EXISTING = True


def get_table_columns(conn, table_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def filtered_payload(payload, allowed_columns, drop_id=True):
    data = dict(payload or {})
    if drop_id:
        data.pop("id", None)
    return {k: v for k, v in data.items() if k in allowed_columns}


def insert_row(conn, table_name, payload):
    if not payload:
        return
    cols = list(payload.keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_sql = ", ".join(cols)
    sql = f"INSERT INTO {table_name} ({col_sql}) VALUES ({placeholders})"
    conn.execute(sql, [payload[c] for c in cols])


def import_old_stock_json(json_path, db_path, replace_existing=True):
    raw = json.loads(Path(json_path).read_text(encoding="utf-8"))
    invoices_blob = raw.get("old_stock_invoices", [])
    if not isinstance(invoices_blob, list):
        raise ValueError("Invalid JSON format: 'old_stock_invoices' must be a list")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        invoices_cols = get_table_columns(conn, "invoices")
        items_cols = get_table_columns(conn, "invoice_items")
        totals_cols = get_table_columns(conn, "invoice_totals")

        conn.execute("BEGIN")

        imported_invoices = 0
        imported_items = 0
        imported_totals = 0

        for block in invoices_blob:
            invoice_data = filtered_payload(block.get("invoice", {}), invoices_cols, drop_id=True)
            if not invoice_data:
                continue

            invoice_number = invoice_data.get("invoice_number")
            if not invoice_number:
                continue

            if replace_existing:
                conn.execute("DELETE FROM invoice_items WHERE invoice_number = ?", (invoice_number,))
                conn.execute("DELETE FROM invoice_totals WHERE invoice_number = ?", (invoice_number,))
                conn.execute("DELETE FROM invoices WHERE invoice_number = ?", (invoice_number,))

            insert_row(conn, "invoices", invoice_data)
            imported_invoices += 1

            for item in block.get("invoice_items", []):
                item_data = filtered_payload(item, items_cols, drop_id=True)
                item_data["invoice_number"] = invoice_number
                insert_row(conn, "invoice_items", item_data)
                imported_items += 1

            for total in block.get("invoice_totals", []):
                total_data = filtered_payload(total, totals_cols, drop_id=True)
                total_data["invoice_number"] = invoice_number
                insert_row(conn, "invoice_totals", total_data)
                imported_totals += 1

        conn.commit()
        return {
            "invoices": imported_invoices,
            "invoice_items": imported_items,
            "invoice_totals": imported_totals,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    result = import_old_stock_json(
        json_path=JSON_PATH,
        db_path=DB_PATH,
        replace_existing=REPLACE_EXISTING,
    )
    print(
        f"Imported invoices={result['invoices']}, "
        f"items={result['invoice_items']}, totals={result['invoice_totals']}"
    )


if __name__ == "__main__":
    main()

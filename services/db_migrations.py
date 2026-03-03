from sqlalchemy import text


def ensure_invoice_totals_tax_columns(engine):
    required_columns = {
        "new_retailer_professional_tax": "REAL",
        "retail_shop_excise_turnover_tax": "REAL",
    }

    with engine.begin() as conn:
        table_exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='invoice_totals'")
        ).fetchone()
        if not table_exists:
            return

        existing_cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(invoice_totals)")).fetchall()
        }

        for col_name, col_type in required_columns.items():
            if col_name not in existing_cols:
                conn.execute(
                    text(f"ALTER TABLE invoice_totals ADD COLUMN {col_name} {col_type}")
                )

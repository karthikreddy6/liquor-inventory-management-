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


def ensure_sell_finance_outside_income_support(engine):
    with engine.begin() as conn:
        finance_table_exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='sell_finance'")
        ).fetchone()
        if finance_table_exists:
            existing_cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(sell_finance)")).fetchall()
            }
            if "total_outside_income" not in existing_cols:
                conn.execute(
                    text("ALTER TABLE sell_finance ADD COLUMN total_outside_income REAL")
                )

        outside_income_table_exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='sell_finance_outside_income'")
        ).fetchone()
        if not outside_income_table_exists:
            conn.execute(text("""
                CREATE TABLE sell_finance_outside_income (
                    id INTEGER PRIMARY KEY,
                    finance_id INTEGER,
                    name VARCHAR,
                    amount REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))


def ensure_user_brand_sort_preferences_support(engine):
    with engine.begin() as conn:
        table_exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='user_brand_sort_preferences'")
        ).fetchone()
        if not table_exists:
            conn.execute(text("""
                CREATE TABLE user_brand_sort_preferences (
                    id INTEGER PRIMARY KEY,
                    username VARCHAR,
                    brand_number VARCHAR,
                    sort_index INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_user_brand_sort_preference UNIQUE (username, brand_number)
                )
            """))

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_user_brand_sort_preferences_username "
            "ON user_brand_sort_preferences (username)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_user_brand_sort_preferences_brand_number "
            "ON user_brand_sort_preferences (brand_number)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_user_brand_sort_preferences_sort_index "
            "ON user_brand_sort_preferences (sort_index)"
        ))


def ensure_user_brand_aliases_support(engine):
    with engine.begin() as conn:
        table_exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='user_brand_aliases'")
        ).fetchone()
        if not table_exists:
            conn.execute(text("""
                CREATE TABLE user_brand_aliases (
                    id INTEGER PRIMARY KEY,
                    username VARCHAR,
                    brand_number VARCHAR,
                    short_name VARCHAR(20),
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_user_brand_alias UNIQUE (username, brand_number)
                )
            """))

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_user_brand_aliases_username "
            "ON user_brand_aliases (username)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_user_brand_aliases_brand_number "
            "ON user_brand_aliases (brand_number)"
        ))

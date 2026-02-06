import sqlite3


def main():
    conn = sqlite3.connect("inventory.db")
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]

    keep = {"price_list", "sqlite_sequence"}

    for t in tables:
        if t in keep:
            continue
        cur.execute(f"DELETE FROM {t}")

    conn.commit()
    conn.close()
    print("cleared all tables except price_list")


if __name__ == "__main__":
    main()

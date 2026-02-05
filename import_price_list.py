import json

from database import SessionLocal
from models import PriceListItem

JSON_PATH = r"mrp_with_size.json"


def to_int(val):
    try:
        return int(val)
    except Exception:
        return 0


def to_float(val):
    try:
        return float(val)
    except Exception:
        return 0.0


def main():
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    parsed = []
    for row in data:
        item_type = str(row.get("type", "")).strip()
        size_code = str(row.get("size_code", "")).strip()
        desc_parts = []
        if item_type:
            desc_parts.append(f"type: {item_type}")
        description = " | ".join(desc_parts)

        parsed.append({
            "brand_number": str(row.get("brand_number", "")).strip(),
            "size_code": size_code,
            "pack_type": str(row.get("pack_type", "")).strip(),
            "product_name": str(row.get("product_name", "")).strip(),
            "mrp": to_float(row.get("mrp", 0)),
            "volume_ml": to_int(row.get("botel_pack_quantity(ml)", 0)),
            "description": description,
        })

    unique = {}
    for item in parsed:
        key = (item["brand_number"], item["size_code"], item["pack_type"], item["volume_ml"])
        unique[key] = item

    db = SessionLocal()
    try:
        inserted = 0
        updated = 0
        for item in unique.values():
            existing = db.query(PriceListItem).filter(
                PriceListItem.brand_number == item["brand_number"],
                PriceListItem.size_code == item["size_code"],
                PriceListItem.pack_type == item["pack_type"],
                PriceListItem.volume_ml == item["volume_ml"],
            ).first()
            if existing:
                existing.product_name = item["product_name"]
                existing.mrp = item["mrp"]
                existing.volume_ml = item["volume_ml"]
                existing.size_code = item["size_code"]
                existing.description = item["description"]
                updated += 1
            else:
                db.add(PriceListItem(**item))
                inserted += 1
        db.commit()
    finally:
        db.close()

    print(f"parsed: {len(parsed)}")
    print(f"unique: {len(unique)}")
    print(f"inserted: {inserted}")
    print(f"updated: {updated}")


if __name__ == "__main__":
    main()

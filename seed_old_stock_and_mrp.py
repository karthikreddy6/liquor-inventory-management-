import hashlib
import json
import os
import re

from database import SessionLocal
from models import PresentStockDetail, PriceListItem
from services.stock_service import recalc_stock_summary

TXT_PATH = "Jiila 1 Wines Stock Summary.txt"
MRP_JSON_PATH = "mrp_with_size.json"
OUT_JSON_PATH = os.path.join("output", "jiila_1_wines_stock.json")


def _to_int(val):
    try:
        return int(str(val).replace(",", "").strip())
    except Exception:
        return 0


def _to_float(val):
    try:
        return float(str(val).replace(",", "").strip())
    except Exception:
        return 0.0


def _normalize_space(s):
    return re.sub(r"\s+", " ", str(s or "").strip())


def _parse_volume_ml(item_name):
    m = re.search(r"(\d{2,4})\s*ml\b", item_name, flags=re.IGNORECASE)
    if m:
        return _to_int(m.group(1))

    name_l = item_name.lower()
    # Heuristic mapping for shorthand units in the stock summary.
    if "nips" in name_l:
        return 180
    if "pints" in name_l:
        return 375
    if "qts" in name_l or "qrs" in name_l:
        return 750
    return 0


def _infer_product_type(item_name):
    name_l = item_name.lower()
    if "beer" in name_l or "lager" in name_l or "strong" in name_l:
        return "BEER"
    if "vodka" in name_l:
        return "VODKA"
    if "rum" in name_l:
        return "RUM"
    if "brandy" in name_l:
        return "BRANDY"
    if "whisky" in name_l or "whiskey" in name_l:
        return "WHISKY"
    return "UNKNOWN"


def _make_brand_number(item_name):
    digest = hashlib.md5(item_name.strip().lower().encode("utf-8")).hexdigest()[:8].upper()
    return f"OS-{digest}"


def parse_old_stock_txt(txt_path):
    with open(txt_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    meta = {
        "source_file": txt_path,
        "invoice_no": "",
        "invoice_date": "",
        "location": "",
    }

    for line in lines:
        line = line.strip()
        if line.startswith("Invoice No:"):
            meta["invoice_no"] = _normalize_space(line.split(":", 1)[1])
        elif line.startswith("Invoice Date:"):
            meta["invoice_date"] = _normalize_space(line.split(":", 1)[1])
        elif line.startswith("Location:"):
            meta["location"] = _normalize_space(line.split(":", 1)[1])

    item_re = re.compile(
        r"^\s*(\d+)\.\s*(.*?)\s*\|\s*Qty:\s*([\d,]+)\s*\|\s*Rate:\s*Rs\.\s*([\d,]+\.\d{2})\s*\|\s*Amount:\s*Rs\.\s*([\d,]+\.\d{2})\s*$",
        flags=re.IGNORECASE,
    )

    items = []
    for line in lines:
        m = item_re.match(line)
        if not m:
            continue
        sl_no = _to_int(m.group(1))
        item_name = _normalize_space(m.group(2))
        qty = _to_int(m.group(3))
        rate = _to_float(m.group(4))
        amount = _to_float(m.group(5))

        volume_ml = _parse_volume_ml(item_name)
        product_type = _infer_product_type(item_name)
        brand_number = _make_brand_number(item_name)
        pack_size_case = 1

        items.append({
            "sl_no": sl_no,
            "brand_number": brand_number,
            "brand_name": item_name,
            "product_type": product_type,
            "pack_type": "LEGACY",
            "pack_size_case": pack_size_case,
            "pack_size_quantity_ml": volume_ml,
            "quantity": qty,
            "rate": rate,
            "amount": amount,
        })

    return {"meta": meta, "items": items}


def write_old_stock_json(payload, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def upsert_price_list(db, json_path):
    with open(json_path, "r", encoding="utf-8") as f:
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
            "mrp": _to_float(row.get("mrp", 0)),
            "volume_ml": _to_int(row.get("botel_pack_quantity(ml)", 0)),
            "description": description,
        })

    unique = {}
    for item in parsed:
        key = (item["brand_number"], item["size_code"], item["pack_type"], item["volume_ml"])
        unique[key] = item

    inserted = 0
    updated = 0
    for item in unique.values():
        row = db.query(PriceListItem).filter(
            PriceListItem.brand_number == item["brand_number"],
            PriceListItem.size_code == item["size_code"],
            PriceListItem.pack_type == item["pack_type"],
            PriceListItem.volume_ml == item["volume_ml"],
        ).first()
        if row:
            row.product_name = item["product_name"]
            row.mrp = item["mrp"]
            row.description = item["description"]
            updated += 1
        else:
            db.add(PriceListItem(**item))
            inserted += 1
    return len(parsed), len(unique), inserted, updated


def upsert_old_stock(db, payload):
    invoice_date = payload.get("meta", {}).get("invoice_date", "")
    items = payload.get("items", [])

    inserted = 0
    updated = 0
    for item in items:
        row = db.query(PresentStockDetail).filter(
            PresentStockDetail.brand_number == item["brand_number"],
            PresentStockDetail.pack_size_case == item["pack_size_case"],
            PresentStockDetail.pack_size_quantity_ml == item["pack_size_quantity_ml"],
        ).first()

        total_cases = int(item.get("quantity") or 0)
        total_bottles = 0
        total_amount = _to_float(item.get("amount") or 0.0)

        if row:
            row.brand_name = item["brand_name"]
            row.product_type = item["product_type"]
            row.pack_type = item["pack_type"]
            row.total_cases = total_cases
            row.total_bottles = total_bottles
            row.rate_per_case = _to_float(item.get("rate") or 0.0)
            row.unit_rate_per_bottle = _to_float(item.get("rate") or 0.0)
            row.total_amount = total_amount
            row.last_invoice_date = invoice_date
            row.last_updated_item_name = f"{item['brand_name']} {item['pack_size_quantity_ml']}ml/{item['pack_size_case']}"
            updated += 1
        else:
            db.add(PresentStockDetail(
                brand_number=item["brand_number"],
                brand_name=item["brand_name"],
                product_type=item["product_type"],
                pack_type=item["pack_type"],
                pack_size_case=item["pack_size_case"],
                pack_size_quantity_ml=item["pack_size_quantity_ml"],
                total_cases=total_cases,
                total_bottles=total_bottles,
                rate_per_case=_to_float(item.get("rate") or 0.0),
                unit_rate_per_bottle=_to_float(item.get("rate") or 0.0),
                total_amount=total_amount,
                last_invoice_date=invoice_date,
                last_updated_item_name=f"{item['brand_name']} {item['pack_size_quantity_ml']}ml/{item['pack_size_case']}",
            ))
            inserted += 1

    recalc_stock_summary(db)
    return inserted, updated, len(items)


def main():
    payload = parse_old_stock_txt(TXT_PATH)
    write_old_stock_json(payload, OUT_JSON_PATH)

    db = SessionLocal()
    try:
        parsed, unique, price_inserted, price_updated = upsert_price_list(db, MRP_JSON_PATH)
        stock_inserted, stock_updated, stock_total = upsert_old_stock(db, payload)
        db.commit()
    finally:
        db.close()

    print(f"old_stock_json: {OUT_JSON_PATH}")
    print(f"old_stock_items: {len(payload.get('items', []))}")
    print(f"price_list_parsed: {parsed}")
    print(f"price_list_unique: {unique}")
    print(f"price_list_inserted: {price_inserted}")
    print(f"price_list_updated: {price_updated}")
    print(f"stock_items_total: {stock_total}")
    print(f"stock_inserted: {stock_inserted}")
    print(f"stock_updated: {stock_updated}")


if __name__ == "__main__":
    main()

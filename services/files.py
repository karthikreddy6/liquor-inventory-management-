import os
from werkzeug.utils import secure_filename
from config import INVOICES_FOLDER

def save_invoice_file(upload_path: str, invoice_date: str, invoice_number: str) -> str:
    if not invoice_date:
        invoice_date = "unknown-date"
    base_name = secure_filename(invoice_date)
    if not base_name:
        base_name = "invoice"
    suffix = secure_filename(invoice_number) or "file"
    filename = f"{base_name}-{suffix}.pdf"
    target_path = os.path.join(INVOICES_FOLDER, filename)
    if os.path.exists(target_path):
        counter = 2
        while True:
            filename = f"{base_name}-{suffix}-{counter}.pdf"
            target_path = os.path.join(INVOICES_FOLDER, filename)
            if not os.path.exists(target_path):
                break
            counter += 1
    os.replace(upload_path, target_path)
    return target_path

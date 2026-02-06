import os
import time

INVOICES_FOLDER = "invoices"
os.makedirs(INVOICES_FOLDER, exist_ok=True)

APP_START_TIME = time.time()

ADMIN_USER = os.getenv("ADMIN_USER", "arjun")
ADMIN_PASS = os.getenv("ADMIN_PASS", "10081984")

JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret")
JWT_ALGORITHM = "HS256"
JWT_EXPIRES_MINUTES = int(os.getenv("JWT_EXPIRES_MINUTES", "480"))

OWNER_USER = os.getenv("OWNER_USER", "owner")
OWNER_PASS = os.getenv("OWNER_PASS", "owner6060")
SUPERVISOR_USER = os.getenv("SUPERVISOR_USER", "supervisor")
SUPERVISOR_PASS = os.getenv("SUPERVISOR_PASS", "super6060")

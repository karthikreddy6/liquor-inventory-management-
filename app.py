from flask import Flask
from flask_cors import CORS

from routes.upload import upload_bp
from routes.stock import stock_bp
from routes.admin import admin_bp
from routes.seller import seller_bp
from routes.auth import auth_bp
from routes.sell_report import sell_report_bp
from routes.sell_finance import sell_finance_bp

app = Flask(__name__)
CORS(app)

app.register_blueprint(upload_bp)
app.register_blueprint(stock_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(seller_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(sell_report_bp)
app.register_blueprint(sell_finance_bp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

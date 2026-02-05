from flask import Flask
from flask_cors import CORS

from routes.upload import upload_bp
from routes.stock import stock_bp
from routes.admin import admin_bp
from routes.seller import seller_bp
from routes.auth import auth_bp
from routes.sales import sales_bp

app = Flask(__name__)
CORS(app)

app.register_blueprint(upload_bp)
app.register_blueprint(stock_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(seller_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(sales_bp)

if __name__ == "__main__":
    app.run(app.run(host="0.0.0.0", port=5000, debug=True)
)

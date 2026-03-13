import os
import time
import requests
import threading
import numpy as np
from flask import Flask, jsonify, render_template
from sklearn.ensemble import RandomForestClassifier
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# Хранилище данных для ML
class MarketBrain:
    def __init__(self):
        self.X = [] # Признаки (Order Flow)
        self.y = [] # Результат (UP/DOWN)
        self.model = RandomForestClassifier(n_estimators=50)
        self.is_trained = False
        self.last_signal = {"signal": "HOLD", "conf": 50, "of_score": 0, "imb": 0}

    def get_market_data(self):
        try:
            # Получаем стакан (Depth)
            d_res = requests.get("https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=50").json()
            bids = sum([float(b[1]) for b in d_res['bids']])
            asks = sum([float(a[1]) for a in d_res['asks']])
            imbalance = (bids - asks) / (bids + asks)

            # Получаем агрессивные покупки/продажи
            t_res = requests.get("https://fapi.binance.com/fapi/v1/aggTrades?symbol=BTCUSDT&limit=100").json()
            buys = sum([float(t['q']) for t in t_res if not t['m']])
            sells = sum([float(t['q']) for t in t_res if t['m']])
            vol_ratio = (buys - sells) / (buys + sells)

            return imbalance, vol_ratio
        except:
            return 0, 0

    def update_logic(self):
        while True:
            imb, vol = self.get_market_data()
            score = (imb * 0.4) + (vol * 0.6)
            
            # Логика принятия решения
            if score > 0.12: sig = "UP"
            elif score < -0.12: sig = "DOWN"
            else: sig = "HOLD"

            self.last_signal = {
                "signal": sig,
                "conf": round(abs(score) * 100 + 50, 1),
                "of_score": round(score, 4),
                "imb": round(imb, 3),
                "vol": round(vol, 3),
                "ts": time.time()
            }
            time.sleep(1)

brain = MarketBrain()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/signal')
def api_signal():
    return jsonify(brain.last_signal)

if __name__ == "__main__":
    threading.Thread(target=brain.update_logic, daemon=True).start()
    app.run(host='0.0.0.0', port=8080)

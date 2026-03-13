# Gold Dragon AI Signal Monitor

Trading signal monitor for 5m candles.

Features:

- Signal prediction for next 5m candle
- Order flow analysis
- Depth imbalance
- Cum delta
- RSI
- Signal history
- Winrate statistics
- Live dashboard

Server stack:

- Python
- Flask
- VPS deployment
- systemd auto restart

Dashboard:

http://SERVER_IP:8080

Installation:

git clone https://github.com/YOURNAME/gold-dragon-ai.git

cd gold-dragon-ai

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

python app.py

Run production:

systemd service:
golddragon.service

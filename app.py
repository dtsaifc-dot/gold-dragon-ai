import math
import time
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)

SYMBOL = "BTCUSDT"
TIMEFRAME_SEC = 300
FINAL_SIGNAL_BEFORE_SEC = 10

BINANCE_KLINES = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_DEPTH = "https://fapi.binance.com/fapi/v1/depth"
BINANCE_AGG = "https://fapi.binance.com/fapi/v1/aggTrades"


def utc_ts():
    return time.time()


def iso_utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def candle_open_ts(ts=None):
    if ts is None:
        ts = utc_ts()
    return math.floor(ts / TIMEFRAME_SEC) * TIMEFRAME_SEC


def next_candle_open_ts(ts=None):
    if ts is None:
        ts = utc_ts()
    return candle_open_ts(ts) + TIMEFRAME_SEC


def seconds_to_next_candle(ts=None):
    if ts is None:
        ts = utc_ts()
    return int(next_candle_open_ts(ts) - ts)


class GoldDragonMonitor:
    def __init__(self):
        now = utc_ts()

        self.live = {
            "signal_hint": "WAIT",
            "conf_live": 50.0,
            "score_live": 0.0,
            "price": 0.0,
            "rsi": 50.0,
            "momentum": 0.0,
            "volume_spike": 1.0,
            "imb": 0.0,
            "ratio": 1.0,
            "flow": 0.0,
            "cum_delta_last": 0.0,
            "cum_delta_slope": 0.0,
            "volatility": 0.0,
            "ts": now,
            "ts_utc": iso_utc(now),
            "seconds_to_next_candle": seconds_to_next_candle(now),
            "next_candle_open_ts": next_candle_open_ts(now),
            "next_candle_open_utc": iso_utc(next_candle_open_ts(now)),
        }

        nxt = next_candle_open_ts(now)
        self.final_signal = {
            "status": "WAITING",
            "signal": "WAIT",
            "confidence": 0.0,
            "score": 0.0,
            "created_ts": None,
            "created_utc": None,
            "target_candle_open_ts": nxt,
            "target_candle_open_utc": iso_utc(nxt),
            "target_candle_close_ts": nxt + TIMEFRAME_SEC,
            "target_candle_close_utc": iso_utc(nxt + TIMEFRAME_SEC),
            "entry_price_reference": None,
            "event_id": None,
        }

        self.history = []
        self._frozen_for_target = None
        self._resolved_for_target = None

    def safe_get_json(self, url, params=None):
        r = requests.get(url, params=params, timeout=5)
        r.raise_for_status()
        return r.json()

    def get_klines_features(self):
        data = self.safe_get_json(
            BINANCE_KLINES,
            {"symbol": SYMBOL, "interval": "5m", "limit": 150},
        )

        closes = [float(x[4]) for x in data]
        highs = [float(x[2]) for x in data]
        lows = [float(x[3]) for x in data]
        volumes = [float(x[5]) for x in data]

        last_close = closes[-1]

        momentum_pct = ((closes[-1] - closes[-4]) / closes[-4]) * 100 if closes[-4] else 0.0

        gains = 0.0
        losses = 0.0
        for i in range(len(closes) - 14, len(closes)):
            diff = closes[i] - closes[i - 1]
            if diff > 0:
                gains += diff
            else:
                losses += abs(diff)

        avg_gain = gains / 14
        avg_loss = losses / 14 if losses != 0 else 1e-7
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else volumes[-1]
        volume_spike = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

        ranges = []
        for i in range(len(highs) - 10, len(highs)):
            if closes[i] > 0:
                ranges.append(((highs[i] - lows[i]) / closes[i]) * 100)
        volatility = sum(ranges) / len(ranges) if ranges else 0.0

        return {
            "price": last_close,
            "momentum_pct": momentum_pct,
            "rsi": rsi,
            "volume_spike": volume_spike,
            "volatility": volatility,
        }

    def get_orderflow_features(self):
        depth = self.safe_get_json(
            BINANCE_DEPTH,
            {"symbol": SYMBOL, "limit": 50},
        )

        bids = sum(float(b[1]) for b in depth["bids"])
        asks = sum(float(a[1]) for a in depth["asks"])
        total = bids + asks
        imbalance = (bids - asks) / total if total > 0 else 0.0
        ratio = (bids / asks) if asks > 0 else 1.0

        trades = self.safe_get_json(
            BINANCE_AGG,
            {"symbol": SYMBOL, "limit": 200},
        )

        buy_qty = 0.0
        sell_qty = 0.0
        cum_delta = 0.0
        cum_points = []

        for t in trades:
            q = float(t["q"])
            if t["m"]:
                sell_qty += q
                cum_delta -= q
            else:
                buy_qty += q
                cum_delta += q
            cum_points.append(cum_delta)

        trades_total = buy_qty + sell_qty
        flow = ((buy_qty - sell_qty) / trades_total) if trades_total > 0 else 0.0

        if len(cum_points) >= 2:
            cum_slope = (cum_points[-1] - cum_points[0]) / len(cum_points)
        else:
            cum_slope = 0.0

        return {
            "imbalance": imbalance,
            "ratio": ratio,
            "trade_pressure": flow,
            "cum_delta_last": cum_points[-1] if cum_points else 0.0,
            "cum_delta_slope": cum_slope,
        }

    def calc_signal(self, f):
        momentum_score = max(-1.0, min(1.0, f["momentum_pct"] / 0.35))
        rsi_score = max(-1.0, min(1.0, (f["rsi"] - 50.0) / 15.0))
        volume_score = max(-1.0, min(1.0, (f["volume_spike"] - 1.0) / 0.8))
        imbalance_score = max(-1.0, min(1.0, f["imbalance"] / 0.20))
        pressure_score = max(-1.0, min(1.0, f["trade_pressure"] / 0.20))
        cum_score = max(-1.0, min(1.0, f["cum_delta_slope"] / 0.30))

        if f["volatility"] <= 0.18:
            vol_penalty = 0.0
        elif f["volatility"] <= 0.35:
            vol_penalty = 0.08
        elif f["volatility"] <= 0.55:
            vol_penalty = 0.16
        else:
            vol_penalty = 0.24

        raw_score = (
            0.22 * momentum_score
            + 0.14 * rsi_score
            + 0.10 * volume_score
            + 0.20 * imbalance_score
            + 0.22 * pressure_score
            + 0.12 * cum_score
        )

        final_score = raw_score - vol_penalty if raw_score > 0 else raw_score + vol_penalty

        if final_score >= 0:
            signal_hint = "UP"
        else:
            signal_hint = "DOWN"

        confidence = 45 + min(50, abs(final_score) * 100)
        confidence = min(95, confidence)

        return {
            "signal_hint": signal_hint,
            "confidence": round(confidence, 1),
            "score": round(final_score, 4),
        }

    def update_live(self):
        k = self.get_klines_features()
        of = self.get_orderflow_features()

        features = {
            "price": k["price"],
            "momentum_pct": k["momentum_pct"],
            "rsi": k["rsi"],
            "volume_spike": k["volume_spike"],
            "volatility": k["volatility"],
            "imbalance": of["imbalance"],
            "ratio": of["ratio"],
            "trade_pressure": of["trade_pressure"],
            "cum_delta_last": of["cum_delta_last"],
            "cum_delta_slope": of["cum_delta_slope"],
        }

        sig = self.calc_signal(features)
        now = utc_ts()

        self.live = {
            "signal_hint": sig["signal_hint"],
            "conf_live": sig["confidence"],
            "score_live": sig["score"],
            "price": round(features["price"], 2),
            "rsi": round(features["rsi"], 1),
            "momentum": round(features["momentum_pct"], 2),
            "volume_spike": round(features["volume_spike"], 2),
            "imb": round(features["imbalance"], 3),
            "ratio": round(features["ratio"], 3),
            "flow": round(features["trade_pressure"], 3),
            "cum_delta_last": round(features["cum_delta_last"], 3),
            "cum_delta_slope": round(features["cum_delta_slope"], 3),
            "volatility": round(features["volatility"], 3),
            "ts": now,
            "ts_utc": iso_utc(now),
            "seconds_to_next_candle": seconds_to_next_candle(now),
            "next_candle_open_ts": next_candle_open_ts(now),
            "next_candle_open_utc": iso_utc(next_candle_open_ts(now)),
        }

    def freeze_signal_10s_before(self):
        now = utc_ts()
        sec_left = seconds_to_next_candle(now)
        target_open = next_candle_open_ts(now)

        if sec_left <= FINAL_SIGNAL_BEFORE_SEC and self._frozen_for_target != target_open:
            frozen_signal = self.live["signal_hint"]
            event_id = f"{int(target_open)}-{frozen_signal}"

            self.final_signal = {
                "status": "FROZEN",
                "signal": frozen_signal,
                "confidence": self.live["conf_live"],
                "score": self.live["score_live"],
                "created_ts": now,
                "created_utc": iso_utc(now),
                "target_candle_open_ts": target_open,
                "target_candle_open_utc": iso_utc(target_open),
                "target_candle_close_ts": target_open + TIMEFRAME_SEC,
                "target_candle_close_utc": iso_utc(target_open + TIMEFRAME_SEC),
                "entry_price_reference": self.live["price"],
                "event_id": event_id,
            }

            self._frozen_for_target = target_open
            print(f"[FROZEN] {frozen_signal} conf={self.live['conf_live']} target={iso_utc(target_open)}")

    def resolve_closed_candle(self):
        if self.final_signal["status"] != "FROZEN":
            return

        target_open = self.final_signal["target_candle_open_ts"]
        target_close = self.final_signal["target_candle_close_ts"]

        if utc_ts() < target_close + 2:
            return

        if self._resolved_for_target == target_open:
            return

        data = self.safe_get_json(
            BINANCE_KLINES,
            {"symbol": SYMBOL, "interval": "5m", "limit": 10},
        )

        target_candle = None
        for row in data:
            row_open = int(row[0] / 1000)
            if row_open == target_open:
                target_candle = row
                break

        if not target_candle:
            return

        open_price = float(target_candle[1])
        close_price = float(target_candle[4])

        actual = "UP" if close_price >= open_price else "DOWN"
        predicted = self.final_signal["signal"]
        result = "WIN" if actual == predicted else "LOSS"

        row = {
            "signal_time_utc": self.final_signal["created_utc"],
            "target_open_utc": self.final_signal["target_candle_open_utc"],
            "target_close_utc": self.final_signal["target_candle_close_utc"],
            "predicted": predicted,
            "confidence": self.final_signal["confidence"],
            "score": self.final_signal["score"],
            "entry_reference_price": self.final_signal["entry_price_reference"],
            "candle_open_price": round(open_price, 2),
            "candle_close_price": round(close_price, 2),
            "actual": actual,
            "result": result,
        }

        self.history.insert(0, row)
        self.history = self.history[:300]
        self._resolved_for_target = target_open
        print(f"[RESOLVED] predicted={predicted} actual={actual} result={result}")

    def stats(self):
        total = len(self.history)
        wins = sum(1 for x in self.history if x["result"] == "WIN")
        losses = sum(1 for x in self.history if x["result"] == "LOSS")
        winrate = round((wins / total) * 100, 1) if total else 0.0
        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "winrate": winrate,
        }

    def loop(self):
        while True:
            try:
                self.update_live()
                self.freeze_signal_10s_before()
                self.resolve_closed_candle()
            except Exception as e:
                print("loop error:", e)
            time.sleep(1)


monitor = GoldDragonMonitor()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/signal")
def api_signal():
    return jsonify({
        "live": monitor.live,
        "final": monitor.final_signal,
        "history": monitor.history[:100],
        "stats": monitor.stats(),
    })


if __name__ == "__main__":
    threading.Thread(target=monitor.loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, debug=False)

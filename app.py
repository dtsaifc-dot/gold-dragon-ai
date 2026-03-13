import time
import math
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# =========================
# CONFIG
# =========================
SYMBOL = "BTCUSDT"
TIMEFRAME_SEC = 300              # 5m
FINAL_SIGNAL_BEFORE_SEC = 10     # сигнал за 10 сек до новой свечи
ENTRY_PRICE_MAX = 0.51           # вход 0.51 или лучше
EARLY_EXIT_PRICE = 0.12          # если сигнал неверный -> режем на 0.12
BINANCE_KLINES = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_DEPTH = "https://fapi.binance.com/fapi/v1/depth"
BINANCE_AGG = "https://fapi.binance.com/fapi/v1/aggTrades"

# paper mode = без реальных ордеров, только логика
PAPER_MODE = True

# =========================
# HELPERS
# =========================
def utc_ts():
    return time.time()

def iso_utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def next_candle_open_ts(now_ts=None):
    if now_ts is None:
        now_ts = utc_ts()
    return math.floor(now_ts / TIMEFRAME_SEC) * TIMEFRAME_SEC + TIMEFRAME_SEC

def current_candle_open_ts(now_ts=None):
    if now_ts is None:
        now_ts = utc_ts()
    return math.floor(now_ts / TIMEFRAME_SEC) * TIMEFRAME_SEC

def seconds_to_next_candle(now_ts=None):
    if now_ts is None:
        now_ts = utc_ts()
    return int(next_candle_open_ts(now_ts) - now_ts)

# =========================
# POLYMARKET STUBS
# ЗДЕСЬ ПОТОМ МЕНЯЕМ НА РЕАЛЬНЫЙ API
# =========================
def get_polymarket_entry_price(side: str) -> float:
    """
    Заглушка. Сейчас возвращает fake цену около 0.50.
    side: "UP" -> считаем как YES
          "DOWN" -> считаем как NO
    Потом здесь подставим реальный запрос цены Polymarket.
    """
    # имитация "рыночной" цены
    base = 0.50
    drift = ((time.time() * 1000) % 7) / 1000.0
    return round(base + drift, 3)

def place_polymarket_order(side: str, price: float, amount: float = 1.0):
    """
    Заглушка. Реальный ордер не отправляет.
    """
    print(f"[PAPER ORDER] side={side} entry_price={price} amount={amount}")

def close_polymarket_order(side: str, reason: str, price: float):
    """
    Заглушка. Реальный ордер не закрывает.
    """
    print(f"[PAPER CLOSE] side={side} exit_price={price} reason={reason}")

# =========================
# MARKET BRAIN
# =========================
class MarketBrain:
    def __init__(self):
        self.last_signal = {
            "signal": "HOLD",
            "conf": 50.0,
            "of_score": 0.0,
            "imb": 0.0,
            "vol": 0.0,
            "momentum": 0.0,
            "rsi": 50.0,
            "volume_spike": 1.0,
            "volatility": 0.0,
            "price": 0.0,
            "ts": utc_ts(),
            "seconds_to_next_candle": seconds_to_next_candle(),
            "next_candle_open_ts": next_candle_open_ts(),
            "next_candle_open_utc": iso_utc(next_candle_open_ts()),
        }

        # финальный сигнал на следующую свечу
        self.final_signal = {
            "status": "WAITING",
            "signal": "HOLD",
            "conf": 0.0,
            "target_candle_open_ts": next_candle_open_ts(),
            "target_candle_open_utc": iso_utc(next_candle_open_ts()),
            "created_ts": None,
            "created_utc": None,
            "entry_rule_max_price": ENTRY_PRICE_MAX,
            "early_exit_price": EARLY_EXIT_PRICE,
            "expiry_ts": next_candle_open_ts() + TIMEFRAME_SEC,
            "expiry_utc": iso_utc(next_candle_open_ts() + TIMEFRAME_SEC),
        }

        # состояние сделки
        self.trade_state = {
            "status": "IDLE",            # IDLE / READY / ENTERED / CLOSED / SKIPPED
            "side": None,                # UP / DOWN
            "entry_price": None,
            "entry_ts": None,
            "entry_utc": None,
            "exit_price": None,
            "exit_ts": None,
            "exit_utc": None,
            "exit_reason": None,         # expiry / early_exit / bad_price / no_signal
            "target_candle_open_ts": None,
            "target_candle_open_utc": None,
            "expiry_ts": None,
            "expiry_utc": None,
        }

        self._last_frozen_target = None
        self._last_entry_attempt_target = None

    def safe_get_json(self, url, params=None):
        r = requests.get(url, params=params, timeout=5)
        r.raise_for_status()
        return r.json()

    def get_klines_features(self):
        data = self.safe_get_json(
            BINANCE_KLINES,
            {"symbol": SYMBOL, "interval": "5m", "limit": 120},
        )

        closes = [float(x[4]) for x in data]
        highs = [float(x[2]) for x in data]
        lows = [float(x[3]) for x in data]
        volumes = [float(x[5]) for x in data]

        last_close = closes[-1]

        # momentum за 3 свечи в %
        momentum_pct = ((closes[-1] - closes[-4]) / closes[-4]) * 100 if closes[-4] else 0.0

        # RSI(14)
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

        # volume spike
        avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else volumes[-1]
        volume_spike = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

        # volatility = средний range 10 свечей в %
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
        depth_total = bids + asks
        imbalance = (bids - asks) / depth_total if depth_total > 0 else 0.0

        trades = self.safe_get_json(
            BINANCE_AGG,
            {"symbol": SYMBOL, "limit": 200},
        )

        aggressive_buy_qty = sum(float(t["q"]) for t in trades if not t["m"])
        aggressive_sell_qty = sum(float(t["q"]) for t in trades if t["m"])
        trades_total = aggressive_buy_qty + aggressive_sell_qty
        trade_pressure = (
            (aggressive_buy_qty - aggressive_sell_qty) / trades_total
            if trades_total > 0
            else 0.0
        )

        return {
            "imbalance": imbalance,
            "trade_pressure": trade_pressure,
        }

    def calculate_signal(self, f):
        momentum_score = max(-1.0, min(1.0, f["momentum_pct"] / 0.35))
        rsi_score = max(-1.0, min(1.0, (f["rsi"] - 50.0) / 15.0))
        volume_score = max(-1.0, min(1.0, (f["volume_spike"] - 1.0) / 0.8))
        imbalance_score = max(-1.0, min(1.0, f["imbalance"] / 0.20))
        pressure_score = max(-1.0, min(1.0, f["trade_pressure"] / 0.20))

        # мягкий штраф за бешеную волу
        if f["volatility"] <= 0.18:
            vol_penalty = 0.0
        elif f["volatility"] <= 0.35:
            vol_penalty = 0.08
        elif f["volatility"] <= 0.55:
            vol_penalty = 0.16
        else:
            vol_penalty = 0.24

        raw_score = (
            0.26 * momentum_score
            + 0.18 * rsi_score
            + 0.12 * volume_score
            + 0.20 * imbalance_score
            + 0.24 * pressure_score
        )

        final_score = raw_score - vol_penalty if raw_score > 0 else raw_score + vol_penalty

        if final_score >= 0.18:
            signal = "UP"
        elif final_score <= -0.18:
            signal = "DOWN"
        else:
            signal = "HOLD"

        confidence = 35 + min(60, abs(final_score) * 90)

        bonus = 0
        if (momentum_score > 0 and pressure_score > 0) or (momentum_score < 0 and pressure_score < 0):
            bonus += 6
        if (imbalance_score > 0 and pressure_score > 0) or (imbalance_score < 0 and pressure_score < 0):
            bonus += 4

        confidence = min(95, confidence + bonus)

        return {
            "signal": signal,
            "confidence": round(confidence, 1),
            "score": round(final_score, 4),
        }

    def build_live_signal(self):
        k = self.get_klines_features()
        of = self.get_orderflow_features()

        features = {
            "price": k["price"],
            "momentum_pct": k["momentum_pct"],
            "rsi": k["rsi"],
            "volume_spike": k["volume_spike"],
            "volatility": k["volatility"],
            "imbalance": of["imbalance"],
            "trade_pressure": of["trade_pressure"],
        }

        result = self.calculate_signal(features)

        now_ts = utc_ts()
        nxt = next_candle_open_ts(now_ts)

        self.last_signal = {
            "signal": result["signal"],
            "conf": result["confidence"],
            "of_score": result["score"],
            "imb": round(features["imbalance"], 3),
            "vol": round(features["trade_pressure"], 3),
            "momentum": round(features["momentum_pct"], 2),
            "rsi": round(features["rsi"], 1),
            "volume_spike": round(features["volume_spike"], 2),
            "volatility": round(features["volatility"], 3),
            "price": round(features["price"], 2),
            "ts": now_ts,
            "ts_utc": iso_utc(now_ts),
            "seconds_to_next_candle": seconds_to_next_candle(now_ts),
            "next_candle_open_ts": nxt,
            "next_candle_open_utc": iso_utc(nxt),
        }

    def freeze_final_signal_if_needed(self):
        now_ts = utc_ts()
        sec_left = seconds_to_next_candle(now_ts)
        target_open = next_candle_open_ts(now_ts)

        # фиксируем 1 раз за 10 сек до новой свечи
        if sec_left <= FINAL_SIGNAL_BEFORE_SEC and self._last_frozen_target != target_open:
            sig = self.last_signal["signal"]
            conf = self.last_signal["conf"]

            self.final_signal = {
                "status": "READY",
                "signal": sig,
                "conf": conf,
                "target_candle_open_ts": target_open,
                "target_candle_open_utc": iso_utc(target_open),
                "created_ts": now_ts,
                "created_utc": iso_utc(now_ts),
                "entry_rule_max_price": ENTRY_PRICE_MAX,
                "early_exit_price": EARLY_EXIT_PRICE,
                "expiry_ts": target_open + TIMEFRAME_SEC,
                "expiry_utc": iso_utc(target_open + TIMEFRAME_SEC),
            }

            self.trade_state = {
                "status": "READY" if sig in ("UP", "DOWN") else "IDLE",
                "side": sig if sig in ("UP", "DOWN") else None,
                "entry_price": None,
                "entry_ts": None,
                "entry_utc": None,
                "exit_price": None,
                "exit_ts": None,
                "exit_utc": None,
                "exit_reason": None,
                "target_candle_open_ts": target_open,
                "target_candle_open_utc": iso_utc(target_open),
                "expiry_ts": target_open + TIMEFRAME_SEC,
                "expiry_utc": iso_utc(target_open + TIMEFRAME_SEC),
            }

            self._last_frozen_target = target_open

            print(
                f"[FINAL SIGNAL] target={iso_utc(target_open)} "
                f"signal={sig} conf={conf} entry<= {ENTRY_PRICE_MAX}"
            )

    def try_enter_trade(self):
        now_ts = utc_ts()
        target_open = self.final_signal["target_candle_open_ts"]

        if self.final_signal["status"] != "READY":
            return

        # пробуем войти только один раз на target candle
        if self._last_entry_attempt_target == target_open:
            return

        if now_ts < target_open:
            return

        self._last_entry_attempt_target = target_open

        side = self.final_signal["signal"]
        if side not in ("UP", "DOWN"):
            self.trade_state["status"] = "SKIPPED"
            self.trade_state["exit_reason"] = "no_signal"
            print("[ENTRY SKIP] no_signal")
            return

        entry_price = get_polymarket_entry_price(side)

        if entry_price <= ENTRY_PRICE_MAX:
            place_polymarket_order(side, entry_price, amount=1.0)
            self.trade_state["status"] = "ENTERED"
            self.trade_state["side"] = side
            self.trade_state["entry_price"] = entry_price
            self.trade_state["entry_ts"] = now_ts
            self.trade_state["entry_utc"] = iso_utc(now_ts)
            print(f"[ENTRY OK] side={side} price={entry_price}")
        else:
            self.trade_state["status"] = "SKIPPED"
            self.trade_state["exit_reason"] = "bad_price"
            self.trade_state["exit_price"] = entry_price
            self.trade_state["exit_ts"] = now_ts
            self.trade_state["exit_utc"] = iso_utc(now_ts)
            print(f"[ENTRY SKIP] bad_price={entry_price} > {ENTRY_PRICE_MAX}")

    def manage_open_trade(self):
        if self.trade_state["status"] != "ENTERED":
            return

        now_ts = utc_ts()
        side = self.trade_state["side"]
        current_contract_price = get_polymarket_entry_price(side)
        expiry_ts = self.trade_state["expiry_ts"]

        # early exit если цена рухнула до 0.12
        if current_contract_price <= EARLY_EXIT_PRICE:
            close_polymarket_order(side, "early_exit", current_contract_price)
            self.trade_state["status"] = "CLOSED"
            self.trade_state["exit_reason"] = "early_exit"
            self.trade_state["exit_price"] = current_contract_price
            self.trade_state["exit_ts"] = now_ts
            self.trade_state["exit_utc"] = iso_utc(now_ts)
            print(f"[EXIT EARLY] side={side} price={current_contract_price}")
            return

        # закрытие на экспирации
        if now_ts >= expiry_ts:
            close_polymarket_order(side, "expiry", current_contract_price)
            self.trade_state["status"] = "CLOSED"
            self.trade_state["exit_reason"] = "expiry"
            self.trade_state["exit_price"] = current_contract_price
            self.trade_state["exit_ts"] = now_ts
            self.trade_state["exit_utc"] = iso_utc(now_ts)
            print(f"[EXIT EXPIRY] side={side} price={current_contract_price}")
            return

    def update_loop(self):
        while True:
            try:
                self.build_live_signal()
                self.freeze_final_signal_if_needed()
                self.try_enter_trade()
                self.manage_open_trade()

                print(
                    f"[{time.strftime('%H:%M:%S')}] "
                    f"live={self.last_signal['signal']} "
                    f"conf={self.last_signal['conf']} "
                    f"score={self.last_signal['of_score']} "
                    f"next={self.last_signal['seconds_to_next_candle']}s "
                    f"final={self.final_signal['signal']} "
                    f"trade={self.trade_state['status']}"
                )

            except Exception as e:
                print("update_loop error:", e)

            time.sleep(2)


brain = MarketBrain()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/signal")
def api_signal():
    return jsonify({
        "live": brain.last_signal,
        "final": brain.final_signal,
        "trade": brain.trade_state,
        # совместимость со старым dashboard
        "signal": brain.last_signal["signal"],
        "conf": brain.last_signal["conf"],
        "of_score": brain.last_signal["of_score"],
        "imb": brain.last_signal["imb"],
        "vol": brain.last_signal["vol"],
        "momentum": brain.last_signal["momentum"],
        "rsi": brain.last_signal["rsi"],
        "volume_spike": brain.last_signal["volume_spike"],
        "volatility": brain.last_signal["volatility"],
        "price": brain.last_signal["price"],
        "seconds_to_next_candle": brain.last_signal["seconds_to_next_candle"],
    })

if __name__ == "__main__":
    threading.Thread(target=brain.update_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, debug=False)

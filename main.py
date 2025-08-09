# main.py — Clean Render/Upbit Bot with MA filter, /status, watchdog

import os
import time
import requests
import pyupbit
import threading
import asyncio
import traceback
from datetime import datetime
from flask import Flask, jsonify

# -------------------------------
# Flask
# -------------------------------
app = Flask(__name__)

@app.route("/")
def index():
    return "✅ Yul Bot is running (Web Service)"

@app.route("/status")
def status():
    try:
        # 즉시 상태 계산(캐시 활용)
        price = pyupbit.get_current_price(SYMBOL)
        ok, last_close, sma_s, sma_l, allow = get_ma_signal()
        # 내부 공유 상태도 포함
        data = {
            "symbol": SYMBOL,
            "price": price,
            "ma_ok": ok,
            "ma_last": last_close,
            "sma_short": sma_s,
            "sma_long": sma_l,
            "allow_buy": allow,
            "bought": BOT_STATE["bought"],
            "buy_price": BOT_STATE["buy_price"],
            "buy_qty": BOT_STATE["buy_qty"],
            "last_trade_time": BOT_STATE["last_trade_time"],
            "last_error": BOT_STATE["last_error"],
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -------------------------------
# ENV & Strategy Params
# -------------------------------
ACCESS_KEY     = os.getenv("ACCESS_KEY")
SECRET_KEY     = os.getenv("SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL        = os.getenv("SYMBOL", "KRW-XRP")  # 바꾸고 싶으면 env로
PROFIT_RATIO  = float(os.getenv("PROFIT_RATIO", "0.03"))  # +3% 익절
LOSS_RATIO    = float(os.getenv("LOSS_RATIO",   "0.01"))  # -1% 손절

USE_MA_FILTER   = os.getenv("USE_MA_FILTER", "true").lower() == "true"
MA_INTERVAL     = os.getenv("MA_INTERVAL", "minute5")  # 5분봉
MA_SHORT        = int(os.getenv("MA_SHORT", "5"))
MA_LONG         = int(os.getenv("MA_LONG", "20"))
MA_REFRESH_SEC  = int(os.getenv("MA_REFRESH_SEC", "30"))

CSV_FILE = os.getenv("CSV_FILE", "trades.csv")

# -------------------------------
# Shared State
# -------------------------------
BOT_STATE = {
    "running": False,
    "bought": False,
    "buy_price": 0.0,
    "buy_qty": 0.0,
    "last_error": None,
    "last_trade_time": None,
}

# MA 캐시
_last_ma_update_ts = 0.0
_cached_ma = {"ok": False, "close": None, "sma_s": None, "sma_l": None}

# -------------------------------
# Telegram (시작/종료/매수/익절/손절/재시작만)
# -------------------------------
def _post_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=5)
    except Exception as e:
        print(f"[텔레그램 전송 실패] {e}")

async def _send_telegram_async(msg: str):
    _post_telegram(msg)

def send_telegram(msg: str):
    try:
        asyncio.run(_send_telegram_async(msg))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_send_telegram_async(msg))
        loop.close()

# -------------------------------
# CSV 저장 (체결내역)
# -------------------------------
def save_trade(side, qty, avg_price, realized_krw, pnl_pct, buy_uuid, sell_uuid, ts):
    import csv
    file_exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, mode='a', newline='') as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow([
                "시간","구분(익절/손절)","체결수량(XRP)","평균체결가",
                "실현손익(원)","손익률(%)","BUY_UUID","SELL_UUID"
            ])
        w.writerow([
            ts, side, f"{qty:.6f}", f"{avg_price:.6f}",
            f"{realized_krw:.2f}", f"{pnl_pct:.2f}", buy_uuid or "", sell_uuid or ""
        ])

# -------------------------------
# 잔고/체결 유틸
# -------------------------------
def get_krw_balance_safely(upbit, retry=3, delay=1.0):
    for _ in range(retry):
        try:
            krw = upbit.get_balance("KRW")
            if krw is not None:
                return krw
        except TypeError as te:
            print(f"[❗TypeError - KRW 잔고] {te}")
        except Exception as e:
            print(f"[❗KRW 잔고 조회 실패] {e}")
        time.sleep(delay)
    return None

def wait_balance_change(getter, before, cmp="gt", timeout=20, interval=0.5):
    waited = 0.0
    while waited < timeout:
        try:
            nowb = getter()
        except Exception:
            nowb = None
        if nowb is not None:
            if cmp == "gt" and nowb > before + 1e-12:
                return nowb
            if cmp == "lt" and nowb < before - 1e-12:
                return nowb
        time.sleep(interval)
        waited += interval
    return None

def avg_price_from_balances_buy(krw_before, krw_after, qty_delta):
    if not qty_delta or qty_delta <= 0:
        return None
    spent = krw_before - krw_after
    if spent <= 0:
        return None
    return spent / qty_delta

def avg_price_from_balances_sell(krw_before, krw_after, qty_delta):
    if not qty_delta or qty_delta <= 0:
        return None
    received = krw_after - krw_before
    if received <= 0:
        return None
    return received / qty_delta

# -------------------------------
# MA Signal (캐시)
# -------------------------------
def get_ma_signal():
    """
    return: (ok, last_close, sma_short, sma_long, allow_buy)
    allow_buy: (last > SMA_short) and (SMA_short > SMA_long)
    """
    global _last_ma_update_ts, _cached_ma

    now_ts = time.time()
    if now_ts - _last_ma_update_ts < MA_REFRESH_SEC and _cached_ma["ok"]:
        c = _cached_ma
        allow = (c["close"] is not None and c["sma_s"] is not None and c["sma_l"] is not None
                 and c["close"] > c["sma_s"] > c["sma_l"])
        return True, c["close"], c["sma_s"], c["sma_l"], allow

    try:
        cnt = max(MA_LONG + 5, 30)
        df = pyupbit.get_ohlcv(SYMBOL, interval=MA_INTERVAL, count=cnt)
        if df is None or df.empty or "close" not in df.columns:
            _cached_ma = {"ok": False, "close": None, "sma_s": None, "sma_l": None}
            _last_ma_update_ts = now_ts
            return False, None, None, None, False

        close = df["close"]
        if len(close) < MA_LONG:
            _cached_ma = {"ok": False, "close": None, "sma_s": None, "sma_l": None}
            _last_ma_update_ts = now_ts
            return False, None, None, None, False

        sma_s = float(close.rolling(MA_SHORT).mean().iloc[-1])
        sma_l = float(close.rolling(MA_LONG).mean().iloc[-1])
        last_close = float(close.iloc[-1])

        _cached_ma = {"ok": True, "close": last_close, "sma_s": sma_s, "sma_l": sma_l}
        _last_ma_update_ts = now_ts

        allow = (last_close > sma_s) and (sma_s > sma_l)
        print(f"[MA] last={last_close:.4f}, SMA{MA_SHORT}={sma_s:.4f}, SMA{MA_LONG}={sma_l:.4f}, allow_buy={allow}")
        return True, last_close, sma_s, sma_l, allow

    except Exception:
        print(f"[MA 예외]\n{traceback.format_exc()}")
        _cached_ma = {"ok": False, "close": None, "sma_s": None, "sma_l": None}
        _last_ma_update_ts = time.time()
        return False, None, None, None, False

# -------------------------------
# Orders
# -------------------------------
def market_buy_all(upbit):
    try:
        krw_before = get_krw_balance_safely(upbit)
        xrp_before = upbit.get_balance("XRP")
        if krw_before is None or krw_before <= 5000:
            print("[BUY] KRW 부족 또는 조회 실패:", krw_before)
            return None, None, None

        spend = krw_before * 0.9995
        r = upbit.buy_market_order(SYMBOL, spend)
        buy_uuid = r.get("uuid") if isinstance(r, dict) else None
        print(f"[BUY] 주문 전송 - KRW 사용 예정: {spend:.2f}, uuid={buy_uuid}")

        xrp_after = wait_balance_change(lambda: upbit.get_balance("XRP"), xrp_before, cmp="gt", timeout=20, interval=0.5)
        if xrp_after is None:
            print("[BUY] 체결 확인 실패 (타임아웃)")
            return None, None, buy_uuid

        filled = xrp_after - xrp_before
        krw_after = get_krw_balance_safely(upbit)
        avg_buy = avg_price_from_balances_buy(krw_before, krw_after, filled)
        print(f"[BUY] 체결 완료 - qty={filled:.6f}, avg={avg_buy:.6f}")
        return avg_buy, filled, buy_uuid

    except Exception:
        print(f"[BUY 예외]\n{traceback.format_exc()}")
        BOT_STATE["last_error"] = f"BUY: {traceback.format_exc()}"
        return None, None, None

def market_sell_all(upbit):
    try:
        xrp_before = upbit.get_balance("XRP")
        krw_before = get_krw_balance_safely(upbit)
        if xrp_before is None or xrp_before <= 0:
            print("[SELL] XRP 부족 또는 조회 실패:", xrp_before)
            return None, None, None, None

        r = upbit.sell_market_order(SYMBOL, xrp_before)
        sell_uuid = r.get("uuid") if isinstance(r, dict) else None
        print(f"[SELL] 주문 전송 - qty={xrp_before:.6f}, uuid={sell_uuid}")

        xrp_after = wait_balance_change(lambda: upbit.get_balance("XRP"), xrp_before, cmp="lt", timeout=20, interval=0.5)
        if xrp_after is None:
            print("[SELL] 체결 확인 실패 (타임아웃)")
            return None, None, None, sell_uuid

        filled = xrp_before - xrp_after
        krw_after = get_krw_balance_safely(upbit)
        avg_sell = avg_price_from_balances_sell(krw_before, krw_after, filled)
        realized_krw = (krw_after - krw_before) if (krw_after is not None and krw_before is not None) else 0.0
        print(f"[SELL] 체결 완료 - qty={filled:.6f}, avg={avg_sell:.6f}, pnl₩={realized_krw:.2f}")
        return avg_sell, filled, realized_krw, sell_uuid

    except Exception:
        print(f"[SELL 예외]\n{traceback.format_exc()}")
        BOT_STATE["last_error"] = f"SELL: {traceback.format_exc()}"
        return None, None, None, None

# -------------------------------
# Main Loop & Supervisor
# -------------------------------
def run_bot_loop():
    upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)
    send_telegram("🤖 자동매매 봇 실행됨 (Web Service)")
    BOT_STATE["running"] = True

    bought = False
    buy_price = 0.0
    buy_qty   = 0.0
    buy_uuid  = None

    while True:
        try:
            price = pyupbit.get_current_price(SYMBOL)
            if price is None:
                time.sleep(2)
                continue

            # 미보유 → 매수
            if not bought:
                allow = True
                if USE_MA_FILTER:
                    ok, last, s, l, allow_buy = get_ma_signal()
                    if not ok or not allow_buy:
                        time.sleep(2)
                        continue
                    allow = allow_buy

                if allow:
                    avg_buy, qty, buuid = market_buy_all(upbit)
                    if avg_buy is not None and qty and qty > 0:
                        bought, buy_price, buy_qty, buy_uuid = True, avg_buy, qty, buuid
                        BOT_STATE.update({"bought": True, "buy_price": buy_price, "buy_qty": buy_qty,
                                          "last_trade_time": datetime.now().isoformat()})
                        send_telegram(f"📥 매수 진입! 평단: {buy_price:.2f} / 수량: {buy_qty:.6f}")
                    else:
                        time.sleep(10)
                else:
                    time.sleep(2)
                continue

            # 보유 중 → 익절/손절
            tp = buy_price * (1 + PROFIT_RATIO)
            sl = buy_price * (1 - LOSS_RATIO)

            if price >= tp or price <= sl:
                avg_sell, qty_sold, realized_krw, suuid = market_sell_all(upbit)
                if avg_sell is not None and qty_sold and qty_sold > 0:
                    pnl_pct = ((avg_sell - buy_price) / buy_price) * 100.0 if buy_price else 0.0
                    is_win = price >= tp
                    msg = "🎯 익절!" if is_win else "💥 손절!"
                    send_telegram(f"{msg} 매도가 평단: {avg_sell:.2f} / 수량: {qty_sold:.6f}")
                    save_trade(
                        side=("익절" if is_win else "손절"),
                        qty=qty_sold,
                        avg_price=avg_sell,
                        realized_krw=realized_krw,
                        pnl_pct=pnl_pct,
                        buy_uuid=buy_uuid,
                        sell_uuid=suuid,
                        ts=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    )
                    bought, buy_price, buy_qty, buy_uuid = False, 0.0, 0.0, None
                    BOT_STATE.update({"bought": False, "buy_price": 0.0, "buy_qty": 0.0,
                                      "last_trade_time": datetime.now().isoformat()})
                else:
                    time.sleep(8)

        except TypeError:
            print(f"[❗TypeError]\n{traceback.format_exc()}")
            BOT_STATE["last_error"] = "TypeError in loop"
            time.sleep(3)  # 계속 진행

        except Exception:
            print(f"[❗루프 예외]\n{traceback.format_exc()}")
            BOT_STATE["last_error"] = "Loop Exception"
            # Supervisor가 재시작하도록 예외 전달
            raise

        time.sleep(2)

def supervisor():
    while True:
        try:
            run_bot_loop()
        except Exception:
            send_telegram("⚠️ 자동매매 루프 예외로 재시작합니다.")
            time.sleep(5)
            continue

# -------------------------------
# Entrypoint
# -------------------------------
if __name__ == "__main__":
    # Supervisor로 봇 백그라운드 실행
    threading.Thread(target=supervisor, daemon=True).start()

    # Flask Run (Render가 PORT를 내려줌)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

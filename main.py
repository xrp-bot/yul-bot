import os
import time
import requests
import pyupbit
import threading
import asyncio
import traceback
from datetime import datetime
from flask import Flask

app = Flask(__name__)

@app.route("/")
def index():
    return "✅ Yul Bot is running (Web Service)"

# -------------------------------
# 환경변수
# -------------------------------
ACCESS_KEY = os.getenv("ACCESS_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# -------------------------------
# 전략 파라미터
# -------------------------------
symbol = "KRW-XRP"
profit_ratio = 0.03
loss_ratio = 0.01

USE_MA_FILTER = True
MA_INTERVAL = "minute5"
MA_SHORT = 5
MA_LONG = 20
MA_REFRESH_SEC = 30

csv_file = "trades.csv"
success_count = 0
fail_count = 0
total_profit_percent = 0.0
_last_ma_update_ts = 0.0
_cached_ma = {"ok": False, "close": None, "sma_s": None, "sma_l": None}

# -------------------------------
# Telegram
# -------------------------------
async def send_telegram_message_async(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, data=payload, timeout=5)
    except Exception as e:
        print(f"[텔레그램 전송 실패] {e}")

def send_telegram_message(msg):
    try:
        asyncio.run(send_telegram_message_async(msg))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(send_telegram_message_async(msg))
        loop.close()

# -------------------------------
# CSV 저장
# -------------------------------
def save_trade(side, qty, avg_price, realized_krw, pnl_pct, buy_uuid, sell_uuid, ts):
    global total_profit_percent, success_count, fail_count
    if side == "익절":
        success_count += 1
        total_profit_percent += pnl_pct
    elif side == "손절":
        fail_count += 1
        total_profit_percent += pnl_pct

    import csv
    file_exists = os.path.isfile(csv_file)
    with open(csv_file, mode='a', newline='') as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow([
                "시간", "구분", "체결수량", "평균체결가",
                "실현손익", "손익률(%)", "BUY_UUID", "SELL_UUID"
            ])
        w.writerow([
            ts, side, f"{qty:.6f}", f"{avg_price:.6f}",
            f"{realized_krw:.2f}", f"{pnl_pct:.2f}", buy_uuid or "", sell_uuid or ""
        ])

# -------------------------------
# 안전한 KRW 잔고 조회
# -------------------------------
def get_krw_balance_safely(upbit, retry=3):
    for _ in range(retry):
        try:
            krw = upbit.get_balance("KRW")
            if krw is not None:
                return krw
        except Exception:
            time.sleep(1)
    return None

# -------------------------------
# 주문 유틸
# -------------------------------
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

def compute_avg_price_from_balances(before, after, qty):
    if qty <= 0:
        return None
    diff = before - after
    if diff <= 0:
        return None
    return diff / qty

# -------------------------------
# 이동평균선 계산
# -------------------------------
def get_ma_signal():
    global _last_ma_update_ts, _cached_ma
    now_ts = time.time()
    if now_ts - _last_ma_update_ts < MA_REFRESH_SEC and _cached_ma["ok"]:
        c = _cached_ma
        allow = c["close"] > c["sma_s"] > c["sma_l"]
        return True, c["close"], c["sma_s"], c["sma_l"], allow

    try:
        df = pyupbit.get_ohlcv(symbol, interval=MA_INTERVAL, count=max(MA_LONG + 5, 30))
        if df is None or "close" not in df.columns:
            _cached_ma = {"ok": False, "close": None, "sma_s": None, "sma_l": None}
            _last_ma_update_ts = now_ts
            return False, None, None, None, False

        close = df["close"]
        sma_s = close.rolling(MA_SHORT).mean().iloc[-1]
        sma_l = close.rolling(MA_LONG).mean().iloc[-1]
        last_close = close.iloc[-1]

        _cached_ma = {"ok": True, "close": float(last_close), "sma_s": float(sma_s), "sma_l": float(sma_l)}
        _last_ma_update_ts = now_ts
        allow = last_close > sma_s > sma_l
        return True, float(last_close), float(sma_s), float(sma_l), allow

    except Exception as e:
        print(f"[MA 오류] {traceback.format_exc()}")
        return False, None, None, None, False

# -------------------------------
# 시장가 매수/매도
# -------------------------------
def market_buy_all(upbit):
    try:
        krw_before = get_krw_balance_safely(upbit)
        if krw_before is None or krw_before <= 5000:
            print("[BUY] KRW 부족 또는 조회 실패:", krw_before)
            return None, None, None

        qty_before = upbit.get_balance("XRP")
        r = upbit.buy_market_order(symbol, krw_before * 0.9995)
        uuid = r.get("uuid") if isinstance(r, dict) else None

        qty_after = wait_balance_change(lambda: upbit.get_balance("XRP"), qty_before, "gt")
        if qty_after is None:
            return None, None, uuid

        avg = compute_avg_price_from_balances(krw_before, upbit.get_balance("KRW"), qty_after - qty_before)
        return avg, qty_after - qty_before, uuid

    except Exception:
        print(f"[BUY 오류] {traceback.format_exc()}")
        return None, None, None

def market_sell_all(upbit):
    try:
        qty_before = upbit.get_balance("XRP")
        krw_before = get_krw_balance_safely(upbit)
        if qty_before is None or qty_before <= 0:
            return None, None, None, None

        r = upbit.sell_market_order(symbol, qty_before)
        uuid = r.get("uuid") if isinstance(r, dict) else None

        qty_after = wait_balance_change(lambda: upbit.get_balance("XRP"), qty_before, "lt")
        if qty_after is None:
            return None, None, None, uuid

        avg = compute_avg_price_from_balances(krw_before, upbit.get_balance("KRW"), qty_before - qty_after)
        profit = upbit.get_balance("KRW") - krw_before
        return avg, qty_before - qty_after, profit, uuid

    except Exception:
        print(f"[SELL 오류] {traceback.format_exc()}")
        return None, None, None, None

# -------------------------------
# 메인 루프
# -------------------------------
def run_bot():
    from signal import signal, SIGINT, SIGTERM
    signal(SIGINT, lambda *_: sys_exit())
    signal(SIGTERM, lambda *_: sys_exit())

    upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)
    send_telegram_message("🤖 자동매매 봇 실행됨 (Web Service)")

    bought = False
    buy_price = buy_qty = 0.0
    buy_uuid = None

    while True:
        try:
            price = pyupbit.get_current_price(symbol)
            if price is None:
                time.sleep(2)
                continue

            if not bought:
                allow_buy = True
                if USE_MA_FILTER:
                    ok, _, _, _, allow = get_ma_signal()
                    if not ok or not allow:
                        time.sleep(2)
                        continue
                    allow_buy = allow

                avg_buy, qty, uuid = market_buy_all(upbit)
                if avg_buy:
                    bought = True
                    buy_price, buy_qty, buy_uuid = avg_buy, qty, uuid
                    send_telegram_message(f"📥 매수 진입! 평단: {avg_buy:.2f} / 수량: {qty:.6f}")
                else:
                    time.sleep(10)
                continue

            # 익절 or 손절
            if price >= buy_price * (1 + profit_ratio) or price <= buy_price * (1 - loss_ratio):
                avg_sell, qty, profit, suuid = market_sell_all(upbit)
                if avg_sell:
                    is_win = price >= buy_price * (1 + profit_ratio)
                    msg = "🎯 익절!" if is_win else "💥 손절!"
                    send_telegram_message(f"{msg} 매도가: {avg_sell:.2f} / 수량: {qty:.6f}")
                    save_trade("익절" if is_win else "손절", qty, avg_sell, profit, ((avg_sell - buy_price) / buy_price) * 100, buy_uuid, suuid, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    bought = False
                    buy_price = buy_qty = 0.0
                    buy_uuid = None
                else:
                    time.sleep(10)

        except Exception:
            print(f"[메인루프 오류] {traceback.format_exc()}")
        time.sleep(2)

# -------------------------------
# 종료 시 알림
# -------------------------------
def sys_exit():
    send_telegram_message("🛑 자동매매 봇 종료됨 (Web Service)")
    os._exit(0)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


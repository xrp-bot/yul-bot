import os
import time
import requests
import pyupbit
import threading
import asyncio
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
profit_ratio = 0.03   # +3% 익절
loss_ratio  = 0.01    # -1% 손절

# 이동평균선 필터
USE_MA_FILTER = True
MA_INTERVAL   = "minute5"  # 5분봉
MA_SHORT      = 5
MA_LONG       = 20
MA_REFRESH_SEC = 30        # MA 재계산 최소 간격(초)

csv_file = "trades.csv"

success_count = 0
fail_count = 0
total_profit_percent = 0.0
last_report_date = None

# MA 캐시
_last_ma_update_ts = 0.0
_cached_ma = {
    "ok": False,
    "close": None,
    "sma_s": None,
    "sma_l": None
}

# -------------------------------
# Telegram (시작/종료/매수/매도만)
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

# ------------------------------------
# CSV 저장 (수량/평단/PnL/UUID)
# ------------------------------------
def save_trade(side, qty, avg_price, realized_krw, pnl_pct, buy_uuid, sell_uuid, ts):
    global total_profit_percent, success_count, fail_count

    if side == "익절":
        success_count += 1
        total_profit_percent += pnl_pct
    elif side == "손절":
        fail_count += 1
        total_profit_percent += pnl_pct

    file_exists = os.path.isfile(csv_file)
    import csv
    with open(csv_file, mode='a', newline='') as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow([
                "시간", "구분(익절/손절)", "체결수량(XRP)", "평균체결가",
                "실현손익(원)", "손익률(%)", "BUY_UUID", "SELL_UUID"
            ])
        w.writerow([
            ts, side, f"{qty:.6f}", f"{avg_price:.6f}",
            f"{realized_krw:.2f}", f"{pnl_pct:.2f}", buy_uuid or "", sell_uuid or ""
        ])

# ------------------------------------
# 체결 확인 유틸 (잔고 변화로 확인)
# ------------------------------------
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

def compute_avg_price_from_balances(krw_before, krw_after, qty_delta):
    if qty_delta <= 0:
        return None
    spent = krw_before - krw_after
    if spent <= 0:
        return None
    return spent / qty_delta

def compute_avg_price_from_balances_sell(krw_before, krw_after, qty_delta):
    if qty_delta <= 0:
        return None
    received = krw_after - krw_before
    if received <= 0:
        return None
    return received / qty_delta

# ------------------------------------
# 이동평균선 계산(캐시, 30초 간격)
# ------------------------------------
def get_ma_signal():
    """
    반환: (ok, last_close, sma_short, sma_long, allow_buy)
      - ok: MA 계산 성공 여부
      - allow_buy: (현재가 > 단기SMA) AND (단기SMA > 장기SMA)
    """
    global _last_ma_update_ts, _cached_ma

    now_ts = time.time()
    if now_ts - _last_ma_update_ts < MA_REFRESH_SEC and _cached_ma["ok"]:
        last_close = _cached_ma["close"]
        ss = _cached_ma["sma_s"]
        sl = _cached_ma["sma_l"]
        allow = (last_close is not None and ss is not None and sl is not None
                 and last_close > ss and ss > sl)
        return True, last_close, ss, sl, allow

    try:
        # MA_LONG + 여유분 만큼 가져오기
        cnt = max(MA_LONG + 5, 30)
        df = pyupbit.get_ohlcv(symbol, interval=MA_INTERVAL, count=cnt)
        if df is None or df.empty or "close" not in df.columns:
            print("[MA] OHLCV 조회 실패")
            _cached_ma = {"ok": False, "close": None, "sma_s": None, "sma_l": None}
            _last_ma_update_ts = now_ts
            return False, None, None, None, False

        close = df["close"]
        if len(close) < MA_LONG:
            print("[MA] 데이터 부족")
            _cached_ma = {"ok": False, "close": None, "sma_s": None, "sma_l": None}
            _last_ma_update_ts = now_ts
            return False, None, None, None, False

        sma_s = close.rolling(MA_SHORT).mean().iloc[-1]
        sma_l = close.rolling(MA_LONG).mean().iloc[-1]
        last_close = close.iloc[-1]

        _cached_ma = {"ok": True, "close": float(last_close), "sma_s": float(sma_s), "sma_l": float(sma_l)}
        _last_ma_update_ts = now_ts

        allow = (last_close > sma_s) and (sma_s > sma_l)
        print(f"[MA] last={last_close:.4f}, SMA{MA_SHORT}={sma_s:.4f}, SMA{MA_LONG}={sma_l:.4f}, allow_buy={allow}")
        return True, float(last_close), float(sma_s), float(sma_l), allow

    except Exception as e:
        print(f"[MA] 예외: {e}")
        _cached_ma = {"ok": False, "close": None, "sma_s": None, "sma_l": None}
        _last_ma_update_ts = now_ts
        return False, None, None, None, False

# ------------------------------------
# 주문 래퍼: 시장가 매수/매도 + 체결 확인 + 로그
# ------------------------------------
def market_buy_all(upbit):
    try:
        krw_before = upbit.get_balance("KRW")
        xrp_before = upbit.get_balance("XRP")
        if krw_before is None or krw_before <= 5000:
            print("[BUY] KRW 부족 또는 조회 실패:", krw_before)
            return None, None, None

        krw_to_spend = krw_before * 0.9995
        r = upbit.buy_market_order(symbol, krw_to_spend)
        buy_uuid = r.get("uuid") if isinstance(r, dict) else None
        print(f"[BUY] 주문 전송 - KRW 사용 예정: {krw_to_spend:.2f}, uuid={buy_uuid}")

        def get_xrp(): return upbit.get_balance("XRP")
        xrp_after = wait_balance_change(get_xrp, xrp_before, cmp="gt", timeout=20, interval=0.5)
        if xrp_after is None:
            print("[BUY] 체결 확인 실패 (타임아웃)")
            return None, None, buy_uuid

        filled_qty = xrp_after - xrp_before
        krw_after = upbit.get_balance("KRW")
        avg_buy = compute_avg_price_from_balances(krw_before, krw_after, filled_qty)
        print(f"[BUY] 체결 완료 - qty={filled_qty:.6f}, avg={avg_buy:.6f}")
        return avg_buy, filled_qty, buy_uuid

    except Exception as e:
        print(f"[BUY] 예외: {e}")
        return None, None, None

def market_sell_all(upbit):
    try:
        xrp_before = upbit.get_balance("XRP")
        krw_before = upbit.get_balance("KRW")
        if xrp_before is None or xrp_before <= 0:
            print("[SELL] XRP 부족 또는 조회 실패:", xrp_before)
            return None, None, None, None

        r = upbit.sell_market_order(symbol, xrp_before)
        sell_uuid = r.get("uuid") if isinstance(r, dict) else None
        print(f"[SELL] 주문 전송 - qty={xrp_before:.6f}, uuid={sell_uuid}")

        def get_xrp(): return upbit.get_balance("XRP")
        xrp_after = wait_balance_change(get_xrp, xrp_before, cmp="lt", timeout=20, interval=0.5)
        if xrp_after is None:
            print("[SELL] 체결 확인 실패 (타임아웃)")
            return None, None, None, sell_uuid

        filled_qty = xrp_before - xrp_after
        krw_after = upbit.get_balance("KRW")
        avg_sell = compute_avg_price_from_balances_sell(krw_before, krw_after, filled_qty)
        realized_krw = (krw_after - krw_before)
        print(f"[SELL] 체결 완료 - qty={filled_qty:.6f}, avg={avg_sell:.6f}, pnl₩={realized_krw:.2f}")
        return avg_sell, filled_qty, realized_krw, sell_uuid

    except Exception as e:
        print(f"[SELL] 예외: {e}")
        return None, None, None, None

# ------------------------------------
# 메인 루프 (알림: 시작/종료/매수/익절/손절만)
# ------------------------------------
def run_bot():
    upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)
    send_telegram_message("🤖 자동매매 봇 실행됨 (Web Service)")
    bought = False
    buy_price = 0.0
    buy_qty   = 0.0
    buy_uuid  = None

    while True:
        try:
            price = pyupbit.get_current_price(symbol)
            if price is None:
                time.sleep(5)
                continue

            # 1) 미보유 -> 매수 시도 (MA 필터)
            if not bought:
                allow_buy = True
                if USE_MA_FILTER:
                    ok, last_close, sma_s, sma_l, allow = get_ma_signal()
                    if not ok:
                        # MA 못 구하면 무리해서 매수하지 않음
                        time.sleep(5)
                        continue
                    allow_buy = allow

                if allow_buy:
                    avg_buy, qty, buuid = market_buy_all(upbit)
                    if avg_buy is not None and qty and qty > 0:
                        bought    = True
                        buy_price = avg_buy
                        buy_qty   = qty
                        buy_uuid  = buuid
                        send_telegram_message(f"📥 매수 진입! 평단: {buy_price:.2f} / 수량: {buy_qty:.6f}")
                    else:
                        # 실패 시 과호출 방지용 대기
                        time.sleep(30)
                else:
                    # 조건 미달 시 대기
                    time.sleep(5)

                continue

            # 2) 보유 중 -> 익절/손절 감시
            target_profit = buy_price * (1 + profit_ratio)
            target_loss   = buy_price * (1 - loss_ratio)

            if price >= target_profit:
                avg_sell, qty_sold, realized_krw, suuid = market_sell_all(upbit)
                if avg_sell is not None and qty_sold and qty_sold > 0:
                    pnl_pct = ((avg_sell - buy_price) / buy_price) * 100.0
                    send_telegram_message(f"🎯 익절! 매도가 평단: {avg_sell:.2f} / 수량: {qty_sold:.6f}")
                    save_trade(
                        side="익절",
                        qty=qty_sold,
                        avg_price=avg_sell,
                        realized_krw=realized_krw,
                        pnl_pct=pnl_pct,
                        buy_uuid=buy_uuid,
                        sell_uuid=suuid,
                        ts=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    )
                    bought = False
                    buy_price = 0.0
                    buy_qty = 0.0
                    buy_uuid = None
                else:
                    time.sleep(10)

            elif price <= target_loss:
                avg_sell, qty_sold, realized_krw, suuid = market_sell_all(upbit)
                if avg_sell is not None and qty_sold and qty_sold > 0:
                    pnl_pct = ((avg_sell - buy_price) / buy_price) * 100.0
                    send_telegram_message(f"💥 손절! 매도가 평단: {avg_sell:.2f} / 수량: {qty_sold:.6f}")
                    save_trade(
                        side="손절",
                        qty=qty_sold,
                        avg_price=avg_sell,
                        realized_krw=realized_krw,
                        pnl_pct=pnl_pct,
                        buy_uuid=buy_uuid,
                        sell_uuid=suuid,
                        ts=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    )
                    bought = False
                    buy_price = 0.0
                    buy_qty = 0.0
                    buy_uuid = None
                else:
                    time.sleep(10)

        except Exception as e:
            # 오류 알림은 미전송, 로그만
            print(f"[LOOP] 예외: {e}")

        # 폴링 간격
        time.sleep(2)

# 서버 종료 알림
import signal
import sys
def signal_handler(sig, frame):
    send_telegram_message("🛑 자동매매 봇 종료됨 (Web Service)")
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

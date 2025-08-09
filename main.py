# main.py — Render/Upbit Bot (robust balances, MA filter, /status, /diag+headers, rate-limit backoff, /health)

import os
import time
import requests
import pyupbit
import threading
import asyncio
import traceback
import uuid
import jwt
from datetime import datetime
from flask import Flask, jsonify

# -------------------------------
# Flask
# -------------------------------
app = Flask(__name__)

@app.route("/")
def index():
    return "✅ Yul Bot is running (Web Service)"

@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": datetime.now().isoformat()}), 200

@app.route("/status")
def status():
    try:
        price = get_price_safe(SYMBOL)
        ok, last_close, sma_s, sma_l, allow = get_ma_signal()
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

# ── 진단용: 업비트 /v1/accounts 원본 콜로 HTTP 상태/본문/헤더 보기
@app.route("/diag")
def diag():
    try:
        status_code, body, headers = call_accounts_raw_with_headers()
        return jsonify({
            "status": status_code,
            "body": body,
            "remaining_req": headers.get("Remaining-Req"),
        })
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

# -------------------------------
# ENV & Strategy Params
# -------------------------------
ACCESS_KEY       = os.getenv("ACCESS_KEY")
SECRET_KEY       = os.getenv("SECRET_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL        = os.getenv("SYMBOL", "KRW-XRP")
PROFIT_RATIO  = float(os.getenv("PROFIT_RATIO", "0.03"))  # +3% 익절
LOSS_RATIO    = float(os.getenv("LOSS_RATIO",   "0.01"))  # -1% 손절

USE_MA_FILTER   = os.getenv("USE_MA_FILTER", "true").lower() == "true"
MA_INTERVAL     = os.getenv("MA_INTERVAL", "minute5")
MA_SHORT        = int(os.getenv("MA_SHORT", "5"))
MA_LONG         = int(os.getenv("MA_LONG", "20"))
MA_REFRESH_SEC  = int(os.getenv("MA_REFRESH_SEC", "60"))  # 호출 줄이기

CSV_FILE = os.getenv("CSV_FILE", "trades.csv")

def _mask(s, keep=4):
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)

# 환경변수 점검 (마스킹만 출력)
print(f"[ENV] ACCESS_KEY={_mask(ACCESS_KEY)} SECRET_KEY={_mask(SECRET_KEY)} SYMBOL={SYMBOL}")

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
# Telegram
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
    # 선택: 텔레그램 백업 로그
    try:
        send_telegram(f"[LOG] {ts} {side} qty={qty:.6f} avg={avg_price:.2f} PnL₩={realized_krw:.0f}({pnl_pct:.2f}%)")
    except Exception:
        pass

# -------------------------------
# 업비트 원본 /v1/accounts 호출 (진단)
# -------------------------------
def call_accounts_raw_with_headers():
    """JWT로 /v1/accounts를 직접 호출해 상태코드/본문/헤더를 반환."""
    if not ACCESS_KEY or not SECRET_KEY:
        raise RuntimeError("ACCESS_KEY/SECRET_KEY not set")
    payload = {'access_key': ACCESS_KEY, 'nonce': str(uuid.uuid4())}
    token = jwt.encode(payload, SECRET_KEY, algorithm='HS256')
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get("https://api.upbit.com/v1/accounts", headers=headers, timeout=10)
    return r.status_code, r.text, r.headers
# Upbit는 JWT 인증 + Remaining-Req 헤더로 레이트리밋을 제공합니다.

# -------------------------------
# 레이트리밋 백오프 (공통)
# -------------------------------
def backoff_from_headers(headers, base=1.0, max_sleep=10.0):
    """
    Remaining-Req: group=default; min=1800; sec=29
    sec 값이 바닥이면 잠깐 대기. 헤더가 없으면 기본 대기.
    """
    rem = headers.get("Remaining-Req") if headers else None
    if rem and "sec=" in rem:
        try:
            sec_left = int(rem.split("sec=")[1].split(";")[0])
            if sec_left <= 1:
                time.sleep(min(base * 2, max_sleep))
                return
        except Exception:
            pass
    time.sleep(base)

# -------------------------------
# 잔고/시세 유틸 (모두 float로 통일, 재시도 포함)
# -------------------------------
def _to_float(x):
    try:
        return float(x)
    except Exception:
        return None

def get_balance_float(upbit, ticker: str, retry=3, delay=0.8):
    """pyupbit.get_balance 래퍼: 항상 float 또는 None을 반환."""
    for i in range(retry):
        try:
            val = upbit.get_balance(ticker)  # 문자열/float/None
            fv = _to_float(val)
            if fv is not None:
                return fv
        except Exception as e:
            print(f"[get_balance_float:{ticker}] {type(e).__name__}: {e}")
        time.sleep(delay * (i + 1))
    return None

def get_krw_balance_safely(upbit, retry=3, delay=0.8):
    return get_balance_float(upbit, "KRW", retry=retry, delay=delay)

def wait_balance_change(getter_float, before_float, cmp="gt", timeout=20, interval=0.5):
    """getter_float는 float 또는 None을 반환해야 함."""
    waited = 0.0
    while waited < timeout:
        nowb = None
        try:
            nowb = getter_float()
        except Exception:
            nowb = None
        if nowb is not None and before_float is not None:
            if cmp == "gt" and nowb > before_float + 1e-12:
                return nowb
            if cmp == "lt" and nowb < before_float - 1e-12:
                return nowb
        time.sleep(interval)
        waited += interval
    return None

def avg_price_from_balances_buy(krw_before, krw_after, qty_delta):
    if not qty_delta or qty_delta <= 0:
        return None
    if krw_before is None or krw_after is None:
        return None
    spent = krw_before - krw_after
    if spent <= 0:
        return None
    return spent / qty_delta

def avg_price_from_balances_sell(krw_before, krw_after, qty_delta):
    if not qty_delta or qty_delta <= 0:
        return None
    if krw_before is None or krw_after is None:
        return None
    received = krw_after - krw_before
    if received <= 0:
        return None
    return received / qty_delta

def get_price_safe(symbol, tries=3, delay=0.6):
    for i in range(tries):
        p = pyupbit.get_current_price(symbol)
        if p is not None:
            return float(p)
        time.sleep(delay * (i + 1))
    return None

def get_ohlcv_safe(symbol, interval, count, tries=3, delay=0.8):
    for i in range(tries):
        df = pyupbit.get_ohlcv(symbol, interval=interval, count=count)
        if df is not None and not df.empty:
            return df
        time.sleep(delay * (i + 1))
    return None

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
        df = get_ohlcv_safe(SYMBOL, interval=MA_INTERVAL, count=cnt)
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
# 주문 응답 방어
# -------------------------------
def _extract_uuid(r):
    if isinstance(r, dict):
        if "error" in r:
            print(f"[UPBIT ORDER ERROR] {r['error']}")
            return None
        return r.get("uuid")
    return None

# -------------------------------
# Orders
# -------------------------------
def market_buy_all(upbit):
    try:
        krw_before = get_krw_balance_safely(upbit)
        xrp_before = get_balance_float(upbit, "XRP")
        if krw_before is None or krw_before <= 5000:
            print("[BUY] KRW 부족 또는 조회 실패:", krw_before)
            return None, None, None

        # pyupbit: 시장가 매수 금액은 수수료 제외 금액 → 보수적으로 집행
        spend = krw_before * 0.9990
        r = upbit.buy_market_order(SYMBOL, spend)
        buy_uuid = _extract_uuid(r)
        if buy_uuid is None:
            print("[BUY] 주문 실패 응답:", r)
            return None, None, None
        print(f"[BUY] 주문 전송 - KRW 사용 예정: {spend:.2f}, uuid={buy_uuid}")

        xrp_after = wait_balance_change(lambda: get_balance_float(upbit, "XRP"),
                                        xrp_before, cmp="gt", timeout=30, interval=0.6)
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
        xrp_before = get_balance_float(upbit, "XRP")
        krw_before = get_krw_balance_safely(upbit)
        if xrp_before is None or xrp_before <= 0:
            print("[SELL] XRP 부족 또는 조회 실패:", xrp_before)
            return None, None, None, None

        r = upbit.sell_market_order(SYMBOL, xrp_before)
        sell_uuid = _extract_uuid(r)
        if sell_uuid is None:
            print("[SELL] 주문 실패 응답:", r)
            return None, None, None, None
        print(f"[SELL] 주문 전송 - qty={xrp_before:.6f}, uuid={sell_uuid}")

        xrp_after = wait_balance_change(lambda: get_balance_float(upbit, "XRP"),
                                        xrp_before, cmp="lt", timeout=30, interval=0.6)
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
    if not ACCESS_KEY or not SECRET_KEY:
        raise RuntimeError("ACCESS_KEY/SECRET_KEY missing")

    # 업비트 초기 진단: /v1/accounts 호출 한 번 (상태/헤더 확인)
    try:
        sc, body, headers = call_accounts_raw_with_headers()
        if sc != 200:
            print(f"[DIAG] /v1/accounts 실패: {sc} {body}")
            send_telegram(f"❗️업비트 인증/허용IP/레이트리밋 점검 필요: {sc}")
            backoff_from_headers(headers)
        else:
            print(f"[DIAG] OK Remaining-Req: {headers.get('Remaining-Req')}")
    except Exception as e:
        print(f"[DIAG 예외] {e}")

    upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)
    send_telegram("🤖 자동매매 봇 실행됨 (Web Service)")
    BOT_STATE["running"] = True

    bought = False
    buy_price = 0.0
    buy_qty   = 0.0
    buy_uuid  = None

    while True:
        try:
            price = get_price_safe(SYMBOL)
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
            time.sleep(3)

        except Exception:
            print(f"[❗루프 예외]\n{traceback.format_exc()}")
            BOT_STATE["last_error"] = "Loop Exception"
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
    # 로컬 실행 시 개발서버로 돌림 (Render/운영에선 gunicorn 권장)
    threading.Thread(target=supervisor, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))  # Render 기본 PORT는 10000
    app.run(host="0.0.0.0", port=port)

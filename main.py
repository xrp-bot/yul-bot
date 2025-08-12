# main.py — Upbit Bot
# (Rebound + Early V-Rebound + Continuation Re-entry + Trailing Stop
#  + Durable Position + Daily Report + Safe /status + Full-sell Sweep & Dust Ignore)

import os
import time
import json
import csv
import math
import requests
import pyupbit
import threading
import asyncio
import traceback
import uuid
import jwt
from datetime import datetime, timedelta
from flask import Flask, jsonify, request

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

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
        deep = request.args.get("deep", "0") == "1"

        price = get_price_safe(SYMBOL)
        ok_ma, last_close, sma_s, sma_l = get_ma_values_cached()

        # 신호 진단(무거운 연산은 deep일 때만)
        ok_reb = ok_con = ok_early = None
        why_reb = why_con = why_early = None
        if deep:
            try:
                ok_reb, why_reb = bullish_rebound_signal(SYMBOL, MA_INTERVAL)
            except Exception as e:
                ok_reb, why_reb = None, f"rebound_check_error: {e}"
            try:
                if EARLY_REBOUND:
                    ok_early, why_early = early_rebound_signal(SYMBOL, MA_INTERVAL)
            except Exception as e:
                ok_early, why_early = None, f"early_check_error: {e}"
            try:
                if CONT_REENTRY:
                    ok_con, why_con = continuation_signal(SYMBOL, MA_INTERVAL)
            except Exception as e:
                ok_con, why_con = None, f"cont_check_error: {e}"

        tp = sl = dyn_sl = trail_sl = gap_tp = gap_sl = None
        can_sell = None
        cannot_reason = None
        if BOT_STATE["bought"] and BOT_STATE["buy_price"] > 0 and price:
            tp = BOT_STATE["buy_price"] * (1 + PROFIT_RATIO)
            sl = BOT_STATE["buy_price"] * (1 - LOSS_RATIO)

            if USE_TRAIL and BOT_STATE.get("trail_active", False):
                peak = max(BOT_STATE.get("peak_price", 0.0) or 0.0, price or 0.0)
                trail_sl = peak * (1 - TRAIL_PCT)
                dyn_sl = max(sl, trail_sl)
            else:
                dyn_sl = sl

            gap_tp = ((tp - price) / BOT_STATE["buy_price"]) * 100.0
            gap_sl = ((price - (dyn_sl or sl)) / BOT_STATE["buy_price"]) * 100.0

            if deep:
                try:
                    can_sell, cannot_reason = check_sell_eligibility(price)
                except Exception as e:
                    can_sell, cannot_reason = None, f"eligibility_check_error: {e}"

        data = {
            "symbol": SYMBOL,
            "coin": COIN,
            "price": price,
            "ma_ok": ok_ma,
            "ma_last": last_close,
            "sma_short": sma_s,
            "sma_long": sma_l,

            "signal_rebound_ok": ok_reb,
            "signal_rebound_reason": why_reb,
            "signal_early_ok": ok_early,
            "signal_early_reason": why_early,
            "signal_cont_ok": ok_con,
            "signal_cont_reason": why_con,

            "bought": BOT_STATE["bought"],
            "buy_price": BOT_STATE["buy_price"],
            "buy_qty": BOT_STATE["buy_qty"],
            "tp": tp, "sl": sl, "dynamic_sl": dyn_sl, "trail_sl": trail_sl,
            "trail_active": BOT_STATE.get("trail_active", False),
            "peak_price": BOT_STATE.get("peak_price", 0.0),
            "gap_to_tp_pct": gap_tp, "gap_to_dynsl_pct": gap_sl,

            "can_sell": can_sell,
            "cannot_sell_reason": cannot_reason,

            "last_trade_time": BOT_STATE["last_trade_time"],
            "last_error": BOT_STATE["last_error"],
            "last_signal_time": BOT_STATE["last_signal_time"],
            "last_signal_reason": BOT_STATE.get("last_signal_reason"),

            "report": {"tz": REPORT_TZ, "hour": REPORT_HOUR, "minute": REPORT_MINUTE},
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200

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

SYMBOL          = os.getenv("SYMBOL", "KRW-XRP").upper()

def base_coin(sym: str) -> str:
    try:
        parts = sym.upper().split('-')
        return parts[1] if len(parts) == 2 else sym.upper()
    except Exception:
        return "XRP"

COIN = base_coin(SYMBOL)

PROFIT_RATIO    = float(os.getenv("PROFIT_RATIO", "0.02"))  # TP +2%
LOSS_RATIO      = float(os.getenv("LOSS_RATIO",   "0.01"))  # SL -1%

# MA/Interval
MA_INTERVAL     = os.getenv("MA_INTERVAL", "minute1")
MA_SHORT        = int(os.getenv("MA_SHORT", "5"))
MA_LONG         = int(os.getenv("MA_LONG", "20"))
MA_REFRESH_SEC  = int(os.getenv("MA_REFRESH_SEC", "30"))

CSV_FILE = os.getenv("CSV_FILE", "trades.csv")
POS_FILE = os.getenv("POS_FILE", "pos.json")

# Rebound entry tuning
BULL_COUNT      = int(os.getenv("BULL_COUNT", "1"))
VOL_MA          = int(os.getenv("VOL_MA", "20"))
VOL_BOOST       = float(os.getenv("VOL_BOOST", "1.10"))
INFLECT_REQUIRE = os.getenv("INFLECT_REQUIRE", "true").lower() == "true"
DOWN_BARS       = int(os.getenv("DOWN_BARS", "6"))
SWING_LOOKBACK  = int(os.getenv("SWING_LOOKBACK", "12"))
BREAK_PREVHIGH  = os.getenv("BREAK_PREVHIGH", "true").lower() == "true"

# Early V-rebound
EARLY_REBOUND      = os.getenv("EARLY_REBOUND", "true").lower() == "true"
EARLY_WICK_RATIO   = float(os.getenv("EARLY_WICK_RATIO", "1.5"))
EARLY_VOL_BOOST    = float(os.getenv("EARLY_VOL_BOOST", "1.0"))
EARLY_SWING_LOOKBK = int(os.getenv("EARLY_SWING_LOOKBK", "20"))

# Continuation re-entry
CONT_REENTRY    = os.getenv("CONT_REENTRY", "true").lower() == "true"
CONT_N          = int(os.getenv("CONT_N", "5"))
PB_TOUCH_S      = os.getenv("PB_TOUCH_S", "true").lower() == "true"
VOL_CONT_BOOST  = float(os.getenv("VOL_CONT_BOOST", "1.00"))

# Trailing stop
USE_TRAIL       = os.getenv("USE_TRAIL", "true").lower() == "true"
TRAIL_PCT       = float(os.getenv("TRAIL_PCT", "0.015"))
ARM_AFTER_R     = float(os.getenv("ARM_AFTER_R", "1.0"))

# Daily report
REPORT_TZ      = os.getenv("REPORT_TZ", "Asia/Seoul")
REPORT_HOUR    = int(os.getenv("REPORT_HOUR", "9"))
REPORT_MINUTE  = int(os.getenv("REPORT_MINUTE", "0"))

# SELL safety / rounding
SELL_MIN_KRW   = float(os.getenv("SELL_MIN_KRW", "5000"))
VOL_ROUND      = int(os.getenv("VOL_ROUND", "6"))

# Dust & sweep
DUST_KRW    = float(os.getenv("DUST_KRW", "50"))
DUST_QTY    = float(os.getenv("DUST_QTY", "1e-6"))
SWEEP_RETRY = int(os.getenv("SWEEP_RETRY", "1"))

def _mask(s, keep=4):
    if not s: return ""
    return s[:keep] + "*" * max(0, len(s) - keep)

print(f"[ENV] ACCESS_KEY={_mask(ACCESS_KEY)} SECRET_KEY={_mask(SECRET_KEY)} SYMBOL={SYMBOL} COIN={COIN}")

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
    "last_signal_time": None,
    "last_signal_reason": None,
    "peak_price": 0.0,
    "trail_active": False,
}

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
# CSV 저장
# -------------------------------
def save_trade(side, qty, avg_price, realized_krw, pnl_pct, buy_uuid, sell_uuid, ts):
    file_exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, mode='a', newline='') as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow([
                "시간", f"구분(익절/손절)", f"체결수량({COIN})", "평균체결가",
                "실현손익(원)","손익률(%)","BUY_UUID","SELL_UUID"
            ])
        w.writerow([
            ts, side, f"{qty:.6f}", f"{avg_price:.6f}",
            f"{realized_krw:.2f}", f"{pnl_pct:.2f}", buy_uuid or "", sell_uuid or ""
        ])
    try:
        send_telegram(f"[LOG] {ts} {side} qty={qty:.6f} {COIN} avg={avg_price:.2f} PnL₩={realized_krw:.0f}({pnl_pct:.2f}%)")
    except Exception:
        pass

# -------------------------------
# Upbit /v1/accounts (JWT)
# -------------------------------
def call_accounts_raw_with_headers():
    if not ACCESS_KEY or not SECRET_KEY:
        raise RuntimeError("ACCESS_KEY/SECRET_KEY not set")
    payload = {'access_key': ACCESS_KEY, 'nonce': str(uuid.uuid4())}
    token = jwt.encode(payload, SECRET_KEY, algorithm='HS256')
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get("https://api.upbit.com/v1/accounts", headers=headers, timeout=10)
    return r.status_code, r.text, r.headers

# -------------------------------
# Helpers
# -------------------------------
def backoff_from_headers(headers, base=1.0, max_sleep=10.0):
    rem = headers.get("Remaining-Req") if headers else None
    if rem and "sec=" in rem:
        try:
            sec_left = int(rem.split("sec=")[1].split(";")[0])
            if sec_left <= 1:
                time.sleep(min(base * 2, max_sleep)); return
        except Exception:
            pass
    time.sleep(base)

def _to_float(x):
    try: return float(x)
    except Exception: return None

def get_balance_float(upbit, ticker: str, retry=3, delay=0.8):
    for i in range(retry):
        try:
            val = upbit.get_balance(ticker)
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
    waited = 0.0
    while waited < timeout:
        try:
            nowb = getter_float()
        except Exception:
            nowb = None
        if nowb is not None and before_float is not None:
            if cmp == "gt" and nowb > before_float + 1e-12: return nowb
            if cmp == "lt" and nowb < before_float - 1e-12: return nowb
        time.sleep(interval); waited += interval
    return None

def avg_price_from_balances_buy(krw_before, krw_after, qty_delta):
    if not qty_delta or qty_delta <= 0: return None
    if krw_before is None or krw_after is None: return None
    spent = krw_before - krw_after
    if spent <= 0: return None
    return spent / qty_delta

def avg_price_from_balances_sell(krw_before, krw_after, qty_delta):
    if not qty_delta or qty_delta <= 0: return None
    if krw_before is None or krw_after is None: return None
    received = krw_after - krw_before
    if received <= 0: return None
    return received / qty_delta

def get_price_safe(symbol, tries=3, delay=0.6):
    for i in range(tries):
        p = pyupbit.get_current_price(symbol)
        if p is not None: return float(p)
        time.sleep(delay * (i + 1))
    return None

def get_ohlcv_safe(symbol, interval, count, tries=3, delay=0.8):
    for i in range(tries):
        df = pyupbit.get_ohlcv(symbol, interval=interval, count=count)
        if df is not None and not df.empty and "close" in df.columns:
            return df
        time.sleep(delay * (i + 1))
    return None

# -------------------------------
# MA (cached) — 닫힌 봉 기준
# -------------------------------
def get_ma_values():
    cnt = max(MA_LONG + 10, 60)
    df = get_ohlcv_safe(SYMBOL, interval=MA_INTERVAL, count=cnt)
    if df is None or df.empty or "close" not in df.columns:
        return False, None, None, None
    close = df["close"]
    if len(close) < MA_LONG + 2:
        return False, None, None, None
    sma_s = float(close.rolling(MA_SHORT).mean().iloc[-2])
    sma_l = float(close.rolling(MA_LONG).mean().iloc[-2])
    last_close = float(close.iloc[-2])
    return True, last_close, sma_s, sma_l

def get_ma_values_cached():
    global _last_ma_update_ts, _cached_ma
    now_ts = time.time()
    if now_ts - _last_ma_update_ts < MA_REFRESH_SEC and _cached_ma["ok"]:
        c = _cached_ma; return True, c["close"], c["sma_s"], c["sma_l"]
    ok, last_close, sma_s, sma_l = get_ma_values()
    _cached_ma = {"ok": ok, "close": last_close, "sma_s": sma_s, "sma_l": sma_l}
    _last_ma_update_ts = now_ts
    return ok, last_close, sma_s, sma_l

# -------------------------------
# Entry Signals
# -------------------------------
def bullish_rebound_signal(symbol, interval):
    """
    반등 초입(보수형):
      A) DOWN_BARS 구간에서 SMA_SHORT <= SMA_LONG 다수
      B) 스윙저점: low[-2] ≈ 최근 SWING_LOOKBACK 최저가(±0.15%)
      C) 전환 양봉 + (옵션) 직전 고가 돌파
      D) 단기선 꺾임(INFLECT): s[-2] > s[-3] & s[-3] <= s[-4] (또는 완화)
      E) 거래량 증가: vol[-2] > MA(VOL_MA)*VOL_BOOST
    """
    cnt = max(MA_LONG + VOL_MA + SWING_LOOKBACK + 10, 160)
    df = get_ohlcv_safe(symbol, interval=interval, count=cnt)
    if df is None or df.empty or len(df) < max(MA_LONG, VOL_MA, SWING_LOOKBACK) + 5:
        return False, "df insufficient"

    close = df["close"]; open_ = df["open"]; high = df["high"]; low = df["low"]; vol = df["volume"]
    sma_s_full = close.rolling(MA_SHORT).mean()
    sma_l_full = close.rolling(MA_LONG).mean()
    s_m2 = float(sma_s_full.iloc[-2]); s_m3 = float(sma_s_full.iloc[-3]); s_m4 = float(sma_s_full.iloc[-4])

    rng = range(-2 - DOWN_BARS + 1, -1)
    down_cnt = sum(1 for i in rng if float(sma_s_full.iloc[i]) <= float(sma_l_full.iloc[i]))
    if down_cnt < max(2, DOWN_BARS - 1):
        return False, f"not enough prior downtrend ({down_cnt}/{DOWN_BARS})"

    look_low = float(low.iloc[-SWING_LOOKBACK-2:-2].min())
    if abs(float(low.iloc[-2]) - look_low) > max(1e-8, look_low*0.0015):
        return False, "no swing low at -2"

    if not (float(close.iloc[-2]) > float(open_.iloc[-2])):  # bullish
        return False, "last candle not bullish"
    if BREAK_PREVHIGH and not (float(close.iloc[-2]) > float(high.iloc[-3])):
        return False, "no break of prev high"

    if BULL_COUNT >= 2:
        for k in range(1, BULL_COUNT + 1):
            row = df.iloc[-1 - k]
            if not (float(row["close"]) > float(row["open"])):
                return False, f"need {BULL_COUNT} bullish candles"

    turning_up = (s_m2 > s_m3) and (s_m3 <= s_m4) if INFLECT_REQUIRE else (s_m2 > s_m3)
    if not turning_up:
        return False, "short MA not turning up"

    vol_ma = float(vol.rolling(VOL_MA).mean().iloc[-2])
    if float(vol.iloc[-2]) <= vol_ma * VOL_BOOST:
        return False, "volume not boosted"

    return True, "rebound: downtrend, swingLow, breakHigh, MA turn up, vol up"

def early_rebound_signal(symbol, interval):
    """
    얼리 V-반등(공격형; 동그라미 구간용):
      - -2 캔들 양봉
      - 아랫꼬리 길이 >= EARLY_WICK_RATIO × 몸통
      - 스윙저점 근접(±0.3%) : low[-2] ≈ 최근 EARLY_SWING_LOOKBK 최저가
      - 거래량: vol[-2] ≥ MA(VOL_MA) × EARLY_VOL_BOOST
      - 단기선 회복 시도: SMA_SHORT[-2] ≥ SMA_SHORT[-3] 또는 close[-2] ≥ SMA_SHORT[-2]
    """
    cnt = max(MA_LONG + VOL_MA + EARLY_SWING_LOOKBK + 10, 160)
    df = get_ohlcv_safe(symbol, interval=interval, count=cnt)
    if df is None or df.empty or len(df) < max(MA_LONG, VOL_MA, EARLY_SWING_LOOKBK) + 5:
        return False, "df insufficient"

    close, open_, high, low, vol = df["close"], df["open"], df["high"], df["low"], df["volume"]
    sma_s = close.rolling(MA_SHORT).mean()

    if not (float(close.iloc[-2]) > float(open_.iloc[-2])):  # bullish
        return False, "not bullish(-2)"

    body = abs(float(close.iloc[-2]) - float(open_.iloc[-2])) + 1e-9
    lower_wick = min(float(open_.iloc[-2]), float(close.iloc[-2])) - float(low.iloc[-2])
    if (lower_wick / body) < EARLY_WICK_RATIO:
        return False, "lower wick too small"

    look_low = float(low.iloc[-EARLY_SWING_LOOKBK-2:-2].min())
    if abs(float(low.iloc[-2]) - look_low) > max(1e-8, look_low*0.003):
        return False, "not near swing low"

    vma = float(vol.rolling(VOL_MA).mean().iloc[-2])
    if float(vol.iloc[-2]) < vma * EARLY_VOL_BOOST:
        return False, "volume not enough"

    s_m2 = float(sma_s.iloc[-2]); s_m3 = float(sma_s.iloc[-3])
    if not (s_m2 >= s_m3 or float(close.iloc[-2]) >= s_m2):
        return False, "short MA not recovering"

    return True, "early_v_rebound: long lower wick + swing low + vol + shortMA recover"

def continuation_signal(symbol, interval):
    """
    추세 지속 재진입:
      - 최근 CONT_N봉: SMA_SHORT > SMA_LONG 유지
      - 그 구간에 SMA_SHORT 터치/하회 1회 이상(PB_TOUCH_S)
      - 모멘텀 재개: close[-2] > open[-2] & close[-2] > high[-3]
      - 거래량: vol[-2] ≥ MA(VOL_MA) × VOL_CONT_BOOST
    """
    cnt = max(MA_LONG + VOL_MA + CONT_N + 10, 160)
    df = get_ohlcv_safe(symbol, interval=interval, count=cnt)
    if df is None or df.empty or len(df) < max(MA_LONG, VOL_MA, CONT_N) + 5:
        return False, "df insufficient"

    close, open_, high, low, vol = df["close"], df["open"], df["high"], df["low"], df["volume"]
    sma_s = close.rolling(MA_SHORT).mean()
    sma_l = close.rolling(MA_LONG).mean()

    ok_trend = all(float(sma_s.iloc[-1 - k]) > float(sma_l.iloc[-1 - k]) for k in range(1, CONT_N + 1))
    if not ok_trend:
        return False, "trend not sustained"

    if PB_TOUCH_S:
        touched = any(float(low.iloc[-1 - k]) <= float(sma_s.iloc[-1 - k]) for k in range(1, CONT_N + 1))
        if not touched:
            return False, "no pullback to short MA"

    if not (float(close.iloc[-2]) > float(open_.iloc[-2]) and float(close.iloc[-2]) > float(high.iloc[-3])):
        return False, "no momentum resume"

    vma = float(vol.rolling(VOL_MA).mean().iloc[-2])
    if float(vol.iloc[-2]) < vma * VOL_CONT_BOOST:
        return False, "volume not enough"

    return True, f"continuation: N={CONT_N}, pullback={PB_TOUCH_S}, vol≥{VOL_CONT_BOOST}x"

# -------------------------------
# Sell helpers / rounding / dust
# -------------------------------
def check_sell_eligibility(mkt_price):
    upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)
    coin_bal = get_balance_float(upbit, COIN)
    if coin_bal is None or coin_bal <= 0:
        return False, f"no {COIN} balance"
    if mkt_price is None:
        return False, "no market price"
    est_krw = coin_bal * mkt_price
    if est_krw < SELL_MIN_KRW:
        return False, f"balance value too small ({est_krw:.0f} < {SELL_MIN_KRW:.0f})"
    return True, None

def round_volume(vol: float) -> float:
    if vol is None or vol <= 0:
        return 0.0
    unit = 10 ** VOL_ROUND
    q = math.floor(vol * unit) / unit  # 내림
    return q if q > 0 else 0.0

def is_dust(qty: float, price: float | None) -> bool:
    if qty is None or qty <= 0:
        return True
    if price is None:
        return qty <= DUST_QTY
    return (qty <= DUST_QTY) or ((qty * price) < DUST_KRW)

# -------------------------------
# Orders
# -------------------------------
def market_buy_all(upbit):
    try:
        krw_before = get_krw_balance_safely(upbit)
        coin_before = get_balance_float(upbit, COIN)
        if krw_before is None or krw_before <= SELL_MIN_KRW:
            print("[BUY] KRW 부족 또는 조회 실패:", krw_before)
            return None, None, None

        spend = krw_before * 0.9990
        r = upbit.buy_market_order(SYMBOL, spend)
        buy_uuid = _extract_uuid(r)
        if buy_uuid is None:
            print("[BUY] 주문 실패 응답:", r); return None, None, None
        print(f"[BUY] 주문 전송 - KRW 사용 예정: {spend:.2f}, uuid={buy_uuid}")

        coin_after = wait_balance_change(lambda: get_balance_float(upbit, COIN),
                                         coin_before, cmp="gt", timeout=30, interval=0.6)
        if coin_after is None:
            print("[BUY] 체결 확인 실패 (타임아웃)"); return None, None, buy_uuid

        filled = coin_after - coin_before
        krw_after = get_krw_balance_safely(upbit)
        avg_buy = avg_price_from_balances_buy(krw_before, krw_after, filled)
        print(f"[BUY] 체결 완료 - qty={filled:.6f} {COIN}, avg={avg_buy:.6f}")
        return avg_buy, filled, buy_uuid

    except Exception:
        print(f"[BUY 예외]\n{traceback.format_exc()}")
        BOT_STATE["last_error"] = f"BUY: {traceback.format_exc()}"
        return None, None, None

def market_sell_all(upbit):
    try:
        coin_before = get_balance_float(upbit, COIN)
        krw_before = get_krw_balance_safely(upbit)
        price_now  = get_price_safe(SYMBOL)

        if coin_before is None or coin_before <= 0:
            print(f"[SELL] {COIN} 부족 또는 조회 실패:", coin_before); return None, None, None, None
        if price_now is None:
            print("[SELL] 현재가 조회 실패"); return None, None, None, None

        est_krw = coin_before * price_now
        if est_krw < SELL_MIN_KRW:
            msg = f"[SELL] 최소 주문금액 미만: 보유 평가 {est_krw:.0f}원 < {SELL_MIN_KRW:.0f}원"
            print(msg); send_telegram(msg)
            if is_dust(coin_before, price_now):
                send_telegram(f"🧹 매도불가 먼지 잔고 무시 처리: {coin_before:.6f} {COIN} (≈₩{coin_before*price_now:.0f})")
            return None, None, None, None

        vol = round_volume(coin_before)
        r = upbit.sell_market_order(SYMBOL, vol)
        sell_uuid = _extract_uuid(r)
        if sell_uuid is None:
            print("[SELL] 주문 실패 응답:", r); return None, None, None, None
        print(f"[SELL] 주문 전송 - qty={vol:.6f} {COIN}, uuid={sell_uuid}")

        coin_after = wait_balance_change(lambda: get_balance_float(upbit, COIN),
                                         coin_before, cmp="lt", timeout=30, interval=0.6)
        if coin_after is None:
            print("[SELL] 체결 확인 실패 (타임아웃)")
            coin_after = get_balance_float(upbit, COIN)

        # 스윕 (잔여가 최소주문 이상이면 추가 매도)
        price_now = get_price_safe(SYMBOL) or price_now
        tries = SWEEP_RETRY
        while tries > 0 and coin_after and price_now and (coin_after * price_now) >= SELL_MIN_KRW:
            vol2 = round_volume(coin_after)
            if vol2 <= 0:
                break
            r2 = upbit.sell_market_order(SYMBOL, vol2)
            _ = _extract_uuid(r2)
            print(f"[SELL] 추가 스윕 매도 - qty={vol2:.6f} {COIN}")
            coin_after2 = wait_balance_change(lambda: get_balance_float(upbit, COIN),
                                              coin_after, cmp="lt", timeout=20, interval=0.5)
            coin_after = coin_after2 if coin_after2 is not None else get_balance_float(upbit, COIN)
            tries -= 1

        # 정산
        krw_after = get_krw_balance_safely(upbit)
        filled = (coin_before - (coin_after or 0.0)) if coin_before is not None else None
        avg_sell = (avg_price_from_balances_sell(krw_before, krw_after, filled)
                    if (krw_before is not None and krw_after is not None and filled) else None)
        realized_krw = (krw_after - krw_before) if (krw_after is not None and krw_before is not None) else 0.0
        avg_sell_str = f"{(avg_sell if avg_sell is not None else 0.0):.6f}"
        print(f"[SELL] 체결 완료 - filled={filled:.6f} {COIN} avg={avg_sell_str} pnl₩={realized_krw:.2f}")

        if is_dust(coin_after or 0.0, price_now) and (coin_after or 0.0) > 0:
            send_telegram(f"🧹 미미한 잔여 무시 처리: {(coin_after or 0.0):.6f} {COIN} (≈₩{(coin_after or 0.0)*price_now:.0f})")

        return avg_sell, filled, realized_krw, sell_uuid

    except Exception:
        print(f"[SELL 예외]\n{traceback.format_exc()}")
        BOT_STATE["last_error"] = f"SELL: {traceback.format_exc()}"
        return None, None, None, None

def _extract_uuid(r):
    if isinstance(r, dict):
        if "error" in r:
            print(f"[UPBIT ORDER ERROR] {r['error']}")
            return None
        return r.get("uuid")
    return None

# -------------------------------
# Position persistence
# -------------------------------
def load_pos():
    if not os.path.exists(POS_FILE):
        return None
    try:
        with open(POS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_pos(buy_price, buy_qty):
    try:
        with open(POS_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "buy_price": float(buy_price),
                "buy_qty": float(buy_qty),
                "time": datetime.now().isoformat()
            }, f)
    except Exception as e:
        print(f"[POS SAVE 실패] {e}")

def clear_pos():
    try:
        if os.path.exists(POS_FILE):
            os.remove(POS_FILE)
    except Exception as e:
        print(f"[POS DELETE 실패] {e}")

def reconcile_position(upbit):
    coin_bal = get_balance_float(upbit, COIN)
    price_now = get_price_safe(SYMBOL) or 0.0

    if is_dust(coin_bal or 0.0, price_now):
        BOT_STATE.update({"bought": False, "buy_price": 0.0, "buy_qty": 0.0, "peak_price": 0.0, "trail_active": False})
        clear_pos()
        if (coin_bal or 0.0) > 0:
            send_telegram(f"🧹 복구 중 먼지 무시 처리: {coin_bal:.6f} {COIN} (≈₩{coin_bal*price_now:.0f})")
        return

    if coin_bal is None or coin_bal <= 0:
        BOT_STATE.update({"bought": False, "buy_price": 0.0, "buy_qty": 0.0, "peak_price": 0.0, "trail_active": False})
        clear_pos()
        return
    pos = load_pos()
    if pos:
        BOT_STATE.update({"bought": True, "buy_price": float(pos.get("buy_price", 0.0)), "buy_qty": coin_bal})
    else:
        BOT_STATE.update({"bought": True, "buy_price": price_now, "buy_qty": coin_bal})
        save_pos(price_now, coin_bal)

    BOT_STATE["peak_price"] = BOT_STATE["buy_price"]
    BOT_STATE["trail_active"] = False
    send_telegram(f"♻️ 포지션 복구 — qty={BOT_STATE['buy_qty']:.6f} {COIN}, avg≈{BOT_STATE['buy_price']:.2f}")

# -------------------------------
# Daily 9AM Report
# -------------------------------
def _tznow():
    if ZoneInfo:
        try: return datetime.now(ZoneInfo(REPORT_TZ))
        except Exception: pass
    return datetime.now()

def _read_csv_rows():
    if not os.path.exists(CSV_FILE): return []
    rows = []
    with open(CSV_FILE, newline='') as f:
        r = csv.DictReader(f)
        for row in r: rows.append(row)
    return rows

def _flt(x, default=0.0):
    try: return float(str(x).replace(',', ''))
    except Exception: return default

def summarize_trades(date_str=None):
    rows = _read_csv_rows()
    if not rows:
        return {"count":0,"wins":0,"losses":0,"realized_krw":0.0,"avg_pnl_pct":0.0,"winrate":0.0}
    filt = []
    for row in rows:
        ts = row.get("시간",""); day = ts[:10] if len(ts)>=10 else ""
        if (date_str is None) or (day == date_str): filt.append(row)
    if not filt:
        return {"count":0,"wins":0,"losses":0,"realized_krw":0.0,"avg_pnl_pct":0.0,"winrate":0.0}
    cnt = len(filt)
    wins = sum(1 for r in filt if "익절" in r.get("구분(익절/손절)",""))
    losses = sum(1 for r in filt if "손절" in r.get("구분(익절/손절)",""))
    realized = sum(_flt(r.get("실현손익(원)",0)) for r in filt)
    avg_pct = sum(_flt(r.get("손익률(%)",0)) for r in filt)/cnt if cnt else 0.0
    winrate = (wins/cnt*100.0) if cnt else 0.0
    return {"count":cnt,"wins":wins,"losses":losses,"realized_krw":realized,"avg_pnl_pct":avg_pct,"winrate":winrate}

def build_daily_report():
    now = _tznow()
    today = now.date().strftime("%Y-%m-%d")
    yesterday = (now.date()-timedelta(days=1)).strftime("%Y-%m-%d")
    d_tot, y_tot, all_tot = summarize_trades(today), summarize_trades(yesterday), summarize_trades(None)
    price = get_price_safe(SYMBOL)
    if BOT_STATE["bought"] and BOT_STATE["buy_price"]>0 and price:
        upnl_pct = (price - BOT_STATE["buy_price"]) / BOT_STATE["buy_price"] * 100.0
        pos_line = f"보유중: 평단 {BOT_STATE['buy_price']:.2f}, 수량 {BOT_STATE['buy_qty']:.6f} {COIN}, 현가 {price:.2f}, 미실현 {upnl_pct:.2f}%"
    else:
        pos_line = "보유 포지션: 없음"
    msg = (
        f"📊 [일일 매매결산] {now.strftime('%Y-%m-%d %H:%M')} ({REPORT_TZ})\n"
        f"— 심볼: {SYMBOL}\n\n"
        f"🔹 오늘({today})\n"
        f"  · 거래수: {d_tot['count']} (승 {d_tot['wins']}/패 {d_tot['losses']}, 승률 {d_tot['winrate']:.1f}%)\n"
        f"  · 실현손익: ₩{d_tot['realized_krw']:.0f} / 평균손익률 {d_tot['avg_pnl_pct']:.2f}%\n\n"
        f"🔹 어제({yesterday})\n"
        f"  · 거래수: {y_tot['count']} (승 {y_tot['wins']}/패 {y_tot['losses']}, 승률 {y_tot['winrate']:.1f}%)\n"
        f"  · 실현손익: ₩{y_tot['realized_krw']:.0f} / 평균손익률 {y_tot['avg_pnl_pct']:.2f}%\n\n"
        f"🔹 누적(전체)\n"
        f"  · 총 실현손익: ₩{all_tot['realized_krw']:.0f} / 총 거래수 {all_tot['count']} (승률 {all_tot['winrate']:.1f}%)\n\n"
        f"🔸 {pos_line}\n"
    )
    return msg

def daily_report_scheduler():
    send_telegram("⏰ 일일 리포트 스케줄러 시작")
    while True:
        try:
            now = _tznow()
            target = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)
            if now > target: target = target + timedelta(days=1)
            sleep_sec = (target - now).total_seconds()
            if sleep_sec > 0:
                time.sleep(min(sleep_sec, 60)); continue
            now_check = _tznow()
            if now_check.hour == REPORT_HOUR and now_check.minute == REPORT_MINUTE:
                send_telegram(build_daily_report())
                time.sleep(60)
            else:
                time.sleep(5)
        except Exception:
            print(f"[리포트 스케줄러 예외]\n{traceback.format_exc()}")
            time.sleep(5)

# -------------------------------
# Main Loop & Supervisor
# -------------------------------
def run_bot_loop():
    if not ACCESS_KEY or not SECRET_KEY:
        raise RuntimeError("ACCESS_KEY/SECRET_KEY missing")

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
    reconcile_position(upbit)

    send_telegram("🤖 자동매매 봇 실행됨 (Web Service)")
    BOT_STATE["running"] = True

    while True:
        try:
            price = get_price_safe(SYMBOL)
            if price is None:
                time.sleep(2); continue

            # --- Entry (우선순위: 얼리 V → 반등 초입 → 추세 지속) ---
            if not BOT_STATE["bought"]:
                ok, why = (False, None)

                # 1) 얼리 V-반등 (최우선)
                if EARLY_REBOUND:
                    ok, why = early_rebound_signal(SYMBOL, MA_INTERVAL)

                # 2) (미충족 시) 반등 초입
                if not ok:
                    ok, why = bullish_rebound_signal(SYMBOL, MA_INTERVAL)

                # 3) (그래도 아니면) 추세 지속 재진입
                if not ok and CONT_REENTRY:
                    ok, why = continuation_signal(SYMBOL, MA_INTERVAL)

                if not ok:
                    time.sleep(2); continue

                try:
                    ok_ma, last_close, s_now, l_now = get_ma_values_cached()
                    send_telegram(
                        "🔔 매수 신호 감지\n"
                        f"- MA: last={last_close if last_close else 0:.2f}, "
                        f"SMA{MA_SHORT}={(s_now if s_now else 0):.2f}, "
                        f"SMA{MA_LONG}={(l_now if l_now else 0):.2f}\n"
                        f"- 이유: {why}"
                    )
                except Exception:
                    pass

                avg_buy, qty, buuid = market_buy_all(upbit)
                if avg_buy is not None and qty and qty > 0:
                    BOT_STATE.update({
                        "bought": True,
                        "buy_price": avg_buy,
                        "buy_qty": qty,
                        "last_trade_time": datetime.now().isoformat(),
                        "last_signal_time": datetime.now().isoformat(),
                        "last_signal_reason": why,
                        "peak_price": avg_buy,
                        "trail_active": False,
                    })
                    save_pos(avg_buy, qty)

                    # 텔레그램에 TP/SL/RR
                    tp = avg_buy * (1 + PROFIT_RATIO)
                    sl = avg_buy * (1 - LOSS_RATIO)
                    rr = (PROFIT_RATIO / LOSS_RATIO) if LOSS_RATIO > 0 else 0.0
                    send_telegram(
                        "📥 매수 진입!\n"
                        f"평단: {avg_buy:.2f} / 수량: {qty:.6f} {COIN}\n"
                        f"🎯 TP: {tp:.2f} (+{PROFIT_RATIO*100:.2f}%) | 🛑 SL: {sl:.2f} (-{LOSS_RATIO*100:.2f}%)\n"
                        f"R:R ≈ {rr:.2f}:1"
                    )
                else:
                    time.sleep(8)
                continue

            # --- Exit: TP/SL (+ trailing) ---
            buy_price = BOT_STATE["buy_price"]
            tp = buy_price * (1 + PROFIT_RATIO)
            base_sl = buy_price * (1 - LOSS_RATIO)

            BOT_STATE["peak_price"] = max(BOT_STATE.get("peak_price", 0.0) or 0.0, price)

            if USE_TRAIL and not BOT_STATE.get("trail_active", False):
                if price >= buy_price * (1 + LOSS_RATIO * ARM_AFTER_R):
                    BOT_STATE["trail_active"] = True
                    send_telegram(f"🛡️ 트레일링 활성화: peak={BOT_STATE['peak_price']:.2f}, trail={TRAIL_PCT*100:.2f}%")

            dyn_sl = base_sl
            if USE_TRAIL and BOT_STATE.get("trail_active", False):
                trail_sl = BOT_STATE["peak_price"] * (1 - TRAIL_PCT)
                dyn_sl = max(base_sl, trail_sl)

            if price >= tp or price <= dyn_sl:
                avg_sell, qty_sold, realized_krw, suuid = market_sell_all(upbit)
                if avg_sell is not None and qty_sold and qty_sold > 0:
                    pnl_pct = ((avg_sell - buy_price) / buy_price) * 100.0 if buy_price else 0.0
                    is_win = price >= tp
                    tag = "🎯 익절!" if is_win else ("🛡️ 트레일링 청산" if USE_TRAIL and price > base_sl else "💥 손절!")
                    send_telegram(f"{tag} 매도가 평균가: {avg_sell:.2f} / 수량: {qty_sold:.6f} {COIN}")

                    save_trade(
                        side=("익절" if is_win else "손절"),
                        qty=qty_sold,
                        avg_price=avg_sell,
                        realized_krw=realized_krw,
                        pnl_pct=pnl_pct,
                        buy_uuid=None,
                        sell_uuid=suuid,
                        ts=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    )
                    BOT_STATE.update({"bought": False, "buy_price": 0.0, "buy_qty": 0.0,
                                      "last_trade_time": datetime.now().isoformat(),
                                      "peak_price": 0.0, "trail_active": False})
                    clear_pos()
                else:
                    ok_sell, why_not = check_sell_eligibility(price)
                    if not ok_sell:
                        coin_now = get_balance_float(upbit, COIN)
                        if is_dust(coin_now or 0.0, price):
                            send_telegram(f"🧹 매도 실패했지만 먼지 잔고라 포지션 종료 처리: {coin_now or 0.0:.6f} {COIN}")
                            BOT_STATE.update({"bought": False, "buy_price": 0.0, "buy_qty": 0.0,
                                              "last_trade_time": datetime.now().isoformat(),
                                              "peak_price": 0.0, "trail_active": False})
                            clear_pos()
                        else:
                            send_telegram(f"⚠️ 매도 불가: {why_not}")
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
            time.sleep(5); continue

# -------------------------------
# Import-time start
# -------------------------------
if not getattr(app, "_bot_started", False):
    threading.Thread(target=supervisor, daemon=True).start()
    app._bot_started = True
    print("[BOOT] Supervisor thread started at import")

if not getattr(app, "_report_started", False):
    threading.Thread(target=daily_report_scheduler, daemon=True).start()
    app._report_started = True
    print("[BOOT] Daily report thread started at import")

if __name__ == "__main__":
    if not getattr(app, "_bot_started", False):
        threading.Thread(target=supervisor, daemon=True).start()
        app._bot_started = True
        print("[BOOT] Supervisor thread started via __main__")
    if not getattr(app, "_report_started", False):
        threading.Thread(target=daily_report_scheduler, daemon=True).start()
        app._report_started = True
        print("[BOOT] Daily report thread started via __main__")
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

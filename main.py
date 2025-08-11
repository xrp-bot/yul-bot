# main.py — Upbit Bot (Rebound Entry + Daily 9AM Report + Signal Alerts)
# - Entry (NEW): 바닥 반등 초입(연속 양봉 + 단기MA 꺾임 + 거래량 증가)
# - Exit: TP/SL
# - Infra: Flask (/health, /status, /diag), Telegram, CSV logs, 9AM Daily Report
# - Start: Import-time background threads (bot + report) — gunicorn-safe

import os
import time
import requests
import pyupbit
import threading
import asyncio
import traceback
import uuid
import jwt
import csv
from datetime import datetime, timedelta
from flask import Flask, jsonify

# Python 3.9+ 표준 타임존
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
        price = get_price_safe(SYMBOL)
        ok_ma, last_close, sma_s, sma_l = get_ma_values_cached()

        data = {
            "symbol": SYMBOL,
            "price": price,
            "ma_ok": ok_ma,
            "ma_last": last_close,
            "sma_short": sma_s,
            "sma_long": sma_l,
            "bought": BOT_STATE["bought"],
            "buy_price": BOT_STATE["buy_price"],
            "buy_qty": BOT_STATE["buy_qty"],
            "last_trade_time": BOT_STATE["last_trade_time"],
            "last_error": BOT_STATE["last_error"],
            "last_signal_time": BOT_STATE["last_signal_time"],
            "last_signal_reason": BOT_STATE.get("last_signal_reason"),
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "entry_params": {
                "BULL_COUNT": BULL_COUNT,
                "VOL_MA": VOL_MA,
                "VOL_BOOST": VOL_BOOST,
                "INFLECT_REQUIRE": INFLECT_REQUIRE,
                "DOWN_BARS": DOWN_BARS,
                "SWING_LOOKBACK": SWING_LOOKBACK,
                "BREAK_PREVHIGH": BREAK_PREVHIGH,
                "MA_INTERVAL": MA_INTERVAL,
            },
            "report": {
                "tz": REPORT_TZ,
                "hour": REPORT_HOUR,
                "minute": REPORT_MINUTE,
            }
        }
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

SYMBOL          = os.getenv("SYMBOL", "KRW-XRP")
PROFIT_RATIO    = float(os.getenv("PROFIT_RATIO", "0.03"))  # +3% 익절
LOSS_RATIO      = float(os.getenv("LOSS_RATIO",   "0.01"))  # -1% 손절

# MA/Interval
MA_INTERVAL     = os.getenv("MA_INTERVAL", "minute1")  # 1분봉 권장
MA_SHORT        = int(os.getenv("MA_SHORT", "5"))
MA_LONG         = int(os.getenv("MA_LONG", "20"))
MA_REFRESH_SEC  = int(os.getenv("MA_REFRESH_SEC", "60"))

CSV_FILE = os.getenv("CSV_FILE", "trades.csv")

# --- NEW: Rebound entry tuning params ---
BULL_COUNT      = int(os.getenv("BULL_COUNT", "1"))          # 연속 양봉 개수
VOL_MA          = int(os.getenv("VOL_MA", "20"))             # 거래량 이동평균 길이
VOL_BOOST       = float(os.getenv("VOL_BOOST", "1.10"))      # 거래량 증폭 배율
INFLECT_REQUIRE = os.getenv("INFLECT_REQUIRE", "true").lower() == "true"  # 단기MA 꺾임 강제
DOWN_BARS       = int(os.getenv("DOWN_BARS", "6"))           # 진입 전 하락 구간 길이
SWING_LOOKBACK  = int(os.getenv("SWING_LOOKBACK", "12"))     # 스윙저점 탐색 구간
BREAK_PREVHIGH  = os.getenv("BREAK_PREVHIGH", "true").lower() == "true"   # 직전봉 고가 돌파 요구

# --- NEW: Daily report params ---
REPORT_TZ      = os.getenv("REPORT_TZ", "Asia/Seoul")
REPORT_HOUR    = int(os.getenv("REPORT_HOUR", "9"))
REPORT_MINUTE  = int(os.getenv("REPORT_MINUTE", "0"))

def _mask(s, keep=4):
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)

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
    "last_signal_time": None,
    "last_signal_reason": None,
}

# 캐시
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
    try:
        send_telegram(f"[LOG] {ts} {side} qty={qty:.6f} avg={avg_price:.2f} PnL₩={realized_krw:.0f}({pnl_pct:.2f}%)")
    except Exception:
        pass

# -------------------------------
# 업비트 /v1/accounts (JWT)
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
# Rate-limit backoff
# -------------------------------
def backoff_from_headers(headers, base=1.0, max_sleep=10.0):
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
# Balances/Price utils
# -------------------------------
def _to_float(x):
    try:
        return float(x)
    except Exception:
        return None

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
        if df is not None and not df.empty and "close" in df.columns:
            return df
        time.sleep(delay * (i + 1))
    return None

# -------------------------------
# MA (cached) — 닫힌 봉 기준
# -------------------------------
_last_ma_update_ts = 0.0
_cached_ma = {"ok": False, "close": None, "sma_s": None, "sma_l": None}

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
        c = _cached_ma
        return True, c["close"], c["sma_s"], c["sma_l"]
    ok, last_close, sma_s, sma_l = get_ma_values()
    _cached_ma = {"ok": ok, "close": last_close, "sma_s": sma_s, "sma_l": sma_l}
    _last_ma_update_ts = now_ts
    return ok, last_close, sma_s, sma_l

# -------------------------------
# NEW: 바닥 반등 초입 신호
# -------------------------------
def bullish_rebound_signal(symbol, interval):
    """
    '바닥 반등 초입' 포착:
      A) 직전 하락 추세: 최근 DOWN_BARS개 닫힌봉 구간에서 SMA_SHORT <= SMA_LONG 다수
      B) 스윙저점: 최근 SWING_LOOKBACK 내 최저가가 직전 닫힌봉(-2) 부근
      C) 전환봉: 양봉 && (옵션) 직전봉 고가 돌파
      D) 단기MA 꺾임(상향 전환)
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
    l_m2 = float(sma_l_full.iloc[-2]); l_m3 = float(sma_l_full.iloc[-3])

    rng = range(-2 - DOWN_BARS + 1, -1)
    down_cnt = sum(1 for i in rng if float(sma_s_full.iloc[i]) <= float(sma_l_full.iloc[i]))
    if down_cnt < max(2, DOWN_BARS - 1):
        return False, f"not enough prior downtrend ({down_cnt}/{DOWN_BARS})"

    look_low = float(low.iloc[-SWING_LOOKBACK-2:-2].min())
    is_swing_low = abs(float(low.iloc[-2]) - look_low) <= max(1e-8, look_low*0.0015)  # 0.15%
    if not is_swing_low:
        return False, "no swing low at -2"

    bull_now = float(close.iloc[-2]) > float(open_.iloc[-2])
    if not bull_now:
        return False, "last candle not bullish"
    if BREAK_PREVHIGH and not (float(close.iloc[-2]) > float(high.iloc[-3])):
        return False, "no break of prev high"

    if BULL_COUNT >= 2:
        ok_bulls = True
        for k in range(1, BULL_COUNT + 1):
            row = df.iloc[-1 - k]
            if not (float(row["close"]) > float(row["open"])):
                ok_bulls = False
                break
        if not ok_bulls:
            return False, f"need {BULL_COUNT} bullish candles"

    turning_up = (s_m2 > s_m3) and (s_m3 <= s_m4) if INFLECT_REQUIRE else (s_m2 > s_m3)
    if not turning_up:
        return False, "short MA not turning up"

    vol_ma = float(vol.rolling(VOL_MA).mean().iloc[-2])
    vol_now = float(vol.iloc[-2])
    if not (vol_now > vol_ma * VOL_BOOST):
        return False, f"volume not boosted ({vol_now:.0f} <= {(vol_ma*VOL_BOOST):.0f})"

    why = (f"downtrend={down_cnt}/{DOWN_BARS}, swingLow, breakHigh={BREAK_PREVHIGH}, "
           f"MA turn up, vol↑ {vol_now:.0f}>{(vol_ma*VOL_BOOST):.0f}")
    return True, why

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

        spend = krw_before * 0.9990  # 수수료 고려 보수적 집행
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
# Daily 9AM Report
# -------------------------------
def _tznow():
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(REPORT_TZ))
        except Exception:
            pass
    return datetime.now()

def _read_csv_rows():
    if not os.path.exists(CSV_FILE):
        return []
    rows = []
    with open(CSV_FILE, newline='') as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append(row)
    return rows

def _flt(x, default=0.0):
    try:
        return float(str(x).replace(',', ''))
    except Exception:
        return default

def summarize_trades(date_str=None):
    """
    date_str: 'YYYY-MM-DD' (해당 일자만), None이면 전체
    returns dict
    """
    rows = _read_csv_rows()
    if not rows:
        return {
            "count": 0, "wins": 0, "losses": 0,
            "realized_krw": 0.0, "avg_pnl_pct": 0.0, "winrate": 0.0
        }
    filt = []
    for row in rows:
        ts = row.get("시간", "")
        day = ts[:10] if len(ts) >= 10 else ""
        if (date_str is None) or (day == date_str):
            filt.append(row)
    if not filt:
        return {
            "count": 0, "wins": 0, "losses": 0,
            "realized_krw": 0.0, "avg_pnl_pct": 0.0, "winrate": 0.0
        }

    cnt = len(filt)
    wins = sum(1 for r in filt if "익절" in r.get("구분(익절/손절)", ""))
    losses = sum(1 for r in filt if "손절" in r.get("구분(익절/손절)", ""))
    realized = sum(_flt(r.get("실현손익(원)", 0)) for r in filt)
    avg_pct = sum(_flt(r.get("손익률(%)", 0)) for r in filt) / cnt if cnt else 0.0
    winrate = (wins / cnt * 100.0) if cnt else 0.0

    return {
        "count": cnt, "wins": wins, "losses": losses,
        "realized_krw": realized, "avg_pnl_pct": avg_pct, "winrate": winrate
    }

def build_daily_report():
    now = _tznow()
    today = now.date().strftime("%Y-%m-%d")
    yesterday = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")

    d_tot = summarize_trades(today)
    y_tot = summarize_trades(yesterday)
    all_tot = summarize_trades(None)

    # 현재 포지션 요약
    price = get_price_safe(SYMBOL)
    if BOT_STATE["bought"] and BOT_STATE["buy_price"] > 0 and price:
        upnl_pct = (price - BOT_STATE["buy_price"]) / BOT_STATE["buy_price"] * 100.0
        pos_line = f"보유중: 평단 {BOT_STATE['buy_price']:.2f}, 수량 {BOT_STATE['buy_qty']:.6f}, 현가 {price:.2f}, 미실현 {upnl_pct:.2f}%"
    else:
        pos_line = "보유 포지션: 없음"

    msg = (
        f"📊 [일일 매매결산] {now.strftime('%Y-%m-%d %H:%M')} ({REPORT_TZ})\n"
        f"— 심볼: {SYMBOL}\n"
        f"\n"
        f"🔹 오늘({today})\n"
        f"  · 거래수: {d_tot['count']} (승 {d_tot['wins']}/패 {d_tot['losses']}, 승률 {d_tot['winrate']:.1f}%)\n"
        f"  · 실현손익: ₩{d_tot['realized_krw']:.0f} / 평균손익률 {d_tot['avg_pnl_pct']:.2f}%\n"
        f"\n"
        f"🔹 어제({yesterday})\n"
        f"  · 거래수: {y_tot['count']} (승 {y_tot['wins']}/패 {y_tot['losses']}, 승률 {y_tot['winrate']:.1f}%)\n"
        f"  · 실현손익: ₩{y_tot['realized_krw']:.0f} / 평균손익률 {y_tot['avg_pnl_pct']:.2f}%\n"
        f"\n"
        f"🔹 누적(전체)\n"
        f"  · 총 실현손익: ₩{all_tot['realized_krw']:.0f} / 총 거래수 {all_tot['count']} (승률 {all_tot['winrate']:.1f}%)\n"
        f"\n"
        f"🔸 {pos_line}\n"
    )
    return msg

def daily_report_scheduler():
    send_telegram("⏰ 일일 리포트 스케줄러 시작")
    while True:
        try:
            now = _tznow()
            target = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)
            if now > target:
                target = target + timedelta(days=1)
            sleep_sec = (target - now).total_seconds()
            # 대기
            if sleep_sec > 0:
                time.sleep(min(sleep_sec, 3600))
                continue

            # 정확 시각 도달 체크
            now_check = _tznow()
            if now_check.hour == REPORT_HOUR and now_check.minute == REPORT_MINUTE:
                msg = build_daily_report()
                send_telegram(msg)
                # 다음날로 이동
                time.sleep(60)  # 중복 전송 방지
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

            # --- Entry: Bar-bottom rebound (NEW) ---
            if not bought:
                ok, why = bullish_rebound_signal(SYMBOL, MA_INTERVAL)
                if not ok:
                    time.sleep(2)
                    continue

                try:
                    ok_ma, last_close, s_now, l_now = get_ma_values_cached()
                    send_telegram(
                        "🔔 단기 반등 매수 신호 감지\n"
                        f"- 조건: 연속 양봉 + 단기MA 상향전환 + 거래량 증가\n"
                        f"- MA: last={last_close if last_close else 0:.2f}, "
                        f"SMA{MA_SHORT}={(s_now if s_now else 0):.2f}, "
                        f"SMA{MA_LONG}={(l_now if l_now else 0):.2f}\n"
                        f"- 이유: {why}"
                    )
                except Exception:
                    pass

                avg_buy, qty, buuid = market_buy_all(upbit)
                if avg_buy is not None and qty and qty > 0:
                    bought, buy_price, buy_qty, buy_uuid = True, avg_buy, qty, buuid
                    BOT_STATE.update({
                        "bought": True,
                        "buy_price": buy_price,
                        "buy_qty": buy_qty,
                        "last_trade_time": datetime.now().isoformat(),
                        "last_signal_time": datetime.now().isoformat(),
                        "last_signal_reason": why,
                    })
                    send_telegram(f"📥 매수 진입! 평단: {buy_price:.2f} / 수량: {buy_qty:.6f}")
                else:
                    time.sleep(8)
                continue

            # --- Exit: TP/SL ---
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
# Import-time start (gunicorn-safe)
# -------------------------------
if not getattr(app, "_bot_started", False):
    threading.Thread(target=supervisor, daemon=True).start()
    app._bot_started = True
    print("[BOOT] Supervisor thread started at import")

# Daily report scheduler thread
if not getattr(app, "_report_started", False):
    threading.Thread(target=daily_report_scheduler, daemon=True).start()
    app._report_started = True
    print("[BOOT] Daily report thread started at import")

# -------------------------------
# Entrypoint (local dev)
# -------------------------------
if __name__ == "__main__":
    if not getattr(app, "_bot_started", False):
        threading.Thread(target=supervisor, daemon=True).start()
        app._bot_started = True
        print("[BOOT] Supervisor thread started via __main__")
    if not getattr(app, "_report_started", False):
        threading.Thread(target=daily_report_scheduler, daemon=True).start()
        app._report_started = True
        print("[BOOT] Daily report thread started via __main__")
    port = int(os.environ.get("PORT", 10000))  # Render 기본 PORT는 10000
    app.run(host="0.0.0.0", port=port)

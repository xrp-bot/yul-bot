# main.py — Upbit Multi-Asset Gainer Scanner Bot (24h)
# Spec (2025-08):
# - 24h 급등 스캐너: 거래대금 TOPN(기본 40) + 10분상승률/1분 스파이크 필터(완화 기본값)
# - 비율 배분: 현금 버퍼 10% 유지, 동시 보유 2종목, 잔여현금/잔여슬롯 균등
# - 매도: 손절 -1.2% → 부분익절 50% @ +2.5%(1회) → 트레일링(최고가 -1.5%)
# - 비상 하드스톱: 손익률 ≤ -2.5% 즉시 전량 청산 (슬리피지 확산 방지)
# - 단계형 쿨다운: 부분익절 경험 포지션 30분 / 그 외 90분
# - 리포트: KST 09:00:15, 집계 구간 전일 09:00 ~ 당일 08:59:59.999
# - 텔레그램 알림: 매수/부분익절/트레일/손절/쿨다운 상세 + 10분마다 스캔요약
# - 시스템 보호: 09:00±(앞) N분 신규 진입 스킵
# - Render/Gunicorn 호환: import-time autostart

import os, time, json, csv, math, requests, pyupbit, threading, traceback, uuid, jwt, random
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request

# ================= Flask =================
app = Flask(__name__)

@app.get("/")
def index():
    return "✅ Upbit Gainer Scanner Bot running"

@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": datetime.now().isoformat()}), 200

@app.get("/liveness")
def liveness():
    return jsonify({"ok": True}), 200

@app.get("/portfolio")
def portfolio():
    with POS_LOCK:
        snap = {t: POS[t].copy() for t in POS}
    nowp = {t: get_price_safe(t) for t in snap.keys()}
    return jsonify({"ok": True, "positions": snap, "prices": nowp}), 200

# ===== (Optional) 빠른 점검 라우트 =====
@app.get("/ping_telegram")
def ping_telegram():
    send_telegram("✅ 텔레그램 핑 OK")
    return {"ok": True}, 200

@app.get("/reconcile")
def reconcile():
    try:
        avg_map = get_exchange_avg_map()
        changed = 0
        with POS_LOCK:
            for t, p in list(POS.items()):
                qty_ex = get_balance_coin(t)
                if qty_ex <= 0:
                    if POS[t].get("qty", 0.0) != 0.0:
                        POS[t]["qty"] = 0.0
                        changed += 1
                else:
                    if abs(POS[t].get("qty", 0.0) - qty_ex) > 1e-9:
                        changed += 1
                    POS[t]["qty"] = qty_ex
                    POS[t]["avg"] = avg_map.get(t, p.get("avg", 0.0)) or p.get("avg", 0.0)
        save_pos()
        send_telegram(f"♻️ 재동기화 완료 — 변경 {changed}건")
        return {"ok": True, "changed": changed}, 200
    except Exception as e:
        return {"ok": False, "err": str(e)}, 500

# ================= ENV =================
ACCESS_KEY       = os.getenv("ACCESS_KEY")
SECRET_KEY       = os.getenv("SECRET_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Capital & risk
CASH_BUFFER_PCT        = float(os.getenv("CASH_BUFFER_PCT", "0.10"))  # 10%
MAX_OPEN_POSITIONS     = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
MIN_ORDER_KRW          = float(os.getenv("MIN_ORDER_KRW", "5500"))

# Scanner (완화 기본값)
SCAN_INTERVAL_SEC      = int(os.getenv("SCAN_INTERVAL_SEC", "45"))    # 45초
TOPN_INITIAL           = int(os.getenv("TOPN_INITIAL", "40"))         # TOP 40
VOL_SPIKE_MULT         = float(os.getenv("VOL_SPIKE_MULT", "1.8"))    # ×1.8
LOOKBACK_MIN           = int(os.getenv("LOOKBACK_MIN", "10"))         # 10분
MIN_PCT_UP             = float(os.getenv("MIN_PCT_UP", "3.5"))        # +3.5%
MIN_PRICE_KRW          = float(os.getenv("MIN_PRICE_KRW", "100"))
EXCLUDED_TICKERS       = set([t.strip() for t in os.getenv("EXCLUDED_TICKERS","KRW-BTC,KRW-ETH").split(",") if t.strip()])

# Trailing / TP / SL
SL_PCT                 = float(os.getenv("SL_PCT", "1.2"))            # -1.2%
TP_PCT                 = float(os.getenv("TP_PCT", "2.5"))            # +2.5% (partial 50%)
TRAIL_ACTIVATE_PCT     = float(os.getenv("TRAIL_ACTIVATE_PCT", "1.0"))# +1.0% 후 활성
TRAIL_PCT              = float(os.getenv("TRAIL_PCT", "1.5"))         # 최고가 -1.5%
PARTIAL_TP_RATIO       = float(os.getenv("PARTIAL_TP_RATIO", "0.5"))  # 50%
FEE_RATE               = float(os.getenv("FEE_RATE", "0.0005"))       # 0.05%

# Reporter
REPORT_TZ              = os.getenv("REPORT_TZ", "Asia/Seoul")
REPORT_HOUR            = int(os.getenv("REPORT_HOUR", "9"))
REPORT_MINUTE          = int(os.getenv("REPORT_MINUTE", "0"))
REPORT_SENT_FILE       = os.getenv("REPORT_SENT_FILE", "./last_report_date.txt")
DUST_KRW               = float(os.getenv("DUST_KRW", "5000"))

# Protection & control (★ 신규 추가)
# 매니저 루프 주기 (ms)
MANAGER_TICK_MS        = int(os.getenv("MANAGER_TICK_MS", "400"))     # 기본 0.4초
# 비상 하드스톱 임계치 (%): 손익률 ≤ -HARD_STOP_PCT 이면 즉시 전량 청산
HARD_STOP_PCT          = float(os.getenv("HARD_STOP_PCT", "2.5"))     # 기본 -2.5%
# 09:00 변동성 보호: 09:00부터 n분간 신규진입 스킵
NO_TRADE_MIN_AROUND_9  = int(os.getenv("NO_TRADE_MIN_AROUND_9", "3")) # 기본 3분

# Persistence
PERSIST_DIR            = os.getenv("PERSIST_DIR", "./")
os.makedirs(PERSIST_DIR, exist_ok=True)
CSV_FILE               = os.path.join(PERSIST_DIR, "trades.csv")
POS_FILE               = os.path.join(PERSIST_DIR, "pos.json")

# Rate-limit/backoff
REQ_CHUNK              = 90
SLEEP_TICKER           = 0.10
SLEEP_OHLCV            = 0.10

# ================= Globals =================
KST = timezone(timedelta(hours=9))
UPBIT = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY) if (ACCESS_KEY and SECRET_KEY) else None

POS_LOCK = threading.Lock()
POS: dict[str, dict] = {}          # ticker -> position state
COOLDOWN: dict[str, float] = {}    # ticker -> epoch

BACKOFF = {
    "topn": TOPN_INITIAL,
    "scan_interval": SCAN_INTERVAL_SEC,
    "sleep_ticker": SLEEP_TICKER,
    "sleep_ohlcv": SLEEP_OHLCV,
}

# ================= Telegram =================
def _post_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(text); return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=5)
    except Exception as e:
        print(f"[TG_FAIL] {e} | {text}")

def send_telegram(msg: str): _post_telegram(msg)

# 상세 포맷 헬퍼
def tg_buy(ticker: str, qty: float, avg: float, krw_spent: float):
    send_telegram(
        "🚀 매수 체결\n"
        f"— 심볼: {ticker}\n"
        f"— 수량: {qty:.6f}\n"
        f"— 체결가: ₩{avg:,.4f}\n"
        f"— 투자금액: ₩{krw_spent:,.0f}"
    )

def tg_partial(ticker: str, qty_sold: float, avg_sell: float, realized_krw: float, pnl_pct: float, left_qty: float):
    send_telegram(
        "✅ 부분 익절 매도 (50%)\n"
        f"— 심볼: {ticker}\n"
        f"— 수량: {qty_sold:.6f}\n"
        f"— 매도가: ₩{avg_sell:,.4f}\n"
        f"— 실현수익: ₩{realized_krw:,.0f} ({pnl_pct:.2f}%)\n"
        f"— 잔여 보유: {left_qty:.6f}"
    )

def tg_trailing(ticker: str, avg_sell: float, filled: float, pnl_pct: float):
    send_telegram(
        "📉 트레일링 매도\n"
        f"— 심볼: {ticker}\n"
        f"— 수량: {filled:.6f}\n"
        f"— 매도가: ₩{avg_sell:,.4f}\n"
        f"— 실현손익률: {pnl_pct:.2f}%\n"
        f"— 상태: 포지션 종료"
    )

def tg_stop(ticker: str, avg_sell: float, filled: float, pnl_pct: float):
    send_telegram(
        "⚠️ 손절 매도\n"
        f"— 심볼: {ticker}\n"
        f"— 수량: {filled:.6f}\n"
        f"— 매도가: ₩{avg_sell:,.4f}\n"
        f"— 손익률: {pnl_pct:.2f}%\n"
        f"— 상태: 포지션 종료"
    )

def tg_cooldown(ticker: str, reason: str, minutes: int):
    send_telegram(
        "⏳ 쿨다운 적용\n"
        f"— 심볼: {ticker}\n"
        f"— 종료 사유: {reason}\n"
        f"— 쿨다운: {minutes}분"
    )

# ================= Util / IO =================
def now_kst() -> datetime: return datetime.now(tz=KST)
def now_str() -> str: return now_kst().strftime("%Y-%m-%d %H:%M:%S")

def get_price_safe(ticker, tries=3, delay=0.6):
    for i in range(tries):
        try:
            p = pyupbit.get_current_price(ticker)
            if p is not None: return float(p)
        except Exception as e:
            print(f"[price:{ticker}] {e}")
        time.sleep(delay*(i+1))
    return None

def get_ohlcv_safe(ticker, count=15, interval="minute1", tries=3, delay=0.8):
    for i in range(tries):
        try:
            df = pyupbit.get_ohlcv(ticker, interval=interval, count=count)
            if df is not None and not df.empty: return df
        except Exception as e:
            print(f"[ohlcv:{ticker}] {e}")
        time.sleep(delay*(i+1))
    return None

def save_pos():
    with POS_LOCK:
        obj = {}
        for t, p in POS.items():
            obj[t] = {
                "qty": float(p.get("qty",0.0)),
                "avg": float(p.get("avg",0.0)),
                "entry_ts": p.get("entry_ts"),
                "highest": float(p.get("highest",0.0)),
                "trail_active": bool(p.get("trail_active", False)),
                "partial_tp_done": bool(p.get("partial_tp_done", False)),
                "cooldown_until": float(p.get("cooldown_until", 0.0)),
            }
    tmp = POS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, POS_FILE)

def load_pos():
    if not os.path.exists(POS_FILE): return
    try:
        with open(POS_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return
    with POS_LOCK:
        POS.clear()
        for t, p in obj.items():
            POS[t] = {
                "qty": float(p.get("qty",0.0)),
                "avg": float(p.get("avg",0.0)),
                "entry_ts": p.get("entry_ts"),
                "highest": float(p.get("highest",0.0)),
                "trail_active": bool(p.get("trail_active", False)),
                "partial_tp_done": bool(p.get("partial_tp_done", False)),
                "cooldown_until": float(p.get("cooldown_until", 0.0)),
            }

def append_csv(row: dict):
    header = ["ts","ticker","side","qty","price","krw","fee","pnl_krw","pnl_pct","note"]
    exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if not exists: w.writeheader()
        w.writerow(row)

# ================= Exchange helpers =================
def get_balance_krw():
    try: return float(UPBIT.get_balance("KRW") or 0.0)
    except Exception: return 0.0

def get_balance_coin(ticker):
    try: return float(UPBIT.get_balance(ticker) or 0.0)
    except Exception: return 0.0

def round_qty(q): return max(0.0, round(q, 6))

def buy_market_krw(ticker, krw_amount):
    # Returns avg_price, qty
    try:
        if krw_amount < MIN_ORDER_KRW: return None, None
        krw_before = get_balance_krw()
        coin_before = get_balance_coin(ticker)
        r = UPBIT.buy_market_order(ticker, krw_amount*0.9990)
        uuid_ = (r or {}).get("uuid")
        if not uuid_:
            send_telegram(f"⚠️ BUY 실패: {ticker} resp={r}")
            return None, None
        # poll filled
        t0 = time.time()
        while time.time()-t0 < 30:
            coin_after = get_balance_coin(ticker)
            if coin_after > coin_before + 1e-9: break
            time.sleep(0.6)
        coin_after = get_balance_coin(ticker)
        qty_filled = coin_after - coin_before
        krw_after = get_balance_krw()
        spent = krw_before - krw_after
        avg = spent/qty_filled if qty_filled>0 else None
        return avg, qty_filled
    except Exception as e:
        send_telegram(f"❌ BUY 예외: {ticker} {e}")
        return None, None

def sell_market_ratio(ticker, ratio):
    # Sell specified ratio of current holding
    try:
        qty_before = get_balance_coin(ticker)
        if qty_before <= 0: return None, None, 0.0
        qty = round_qty(qty_before * ratio)
        price_now = get_price_safe(ticker)
        if not price_now: return None, None, 0.0
        if qty*price_now < MIN_ORDER_KRW:
            return None, None, 0.0
        krw_before = get_balance_krw()
        r = UPBIT.sell_market_order(ticker, qty)
        uuid_ = (r or {}).get("uuid")
        if not uuid_:
            send_telegram(f"⚠️ SELL 실패: {ticker} resp={r}")
            return None, None, 0.0
        t0 = time.time()
        while time.time()-t0 < 30:
            qty_after = get_balance_coin(ticker)
            if qty_after < qty_before - 1e-9: break
            time.sleep(0.6)
        qty_after = get_balance_coin(ticker)
        krw_after = get_balance_krw()
        filled = (qty_before - qty_after)
        received = krw_after - krw_before
        avg = received/filled if filled>0 else None
        return avg, filled, received
    except Exception as e:
        send_telegram(f"❌ SELL 예외: {ticker} {e}")
        return None, None, 0.0

def sell_market_all(ticker):
    return sell_market_ratio(ticker, 1.0)

# ================= Rate/backoff =================
def on_rate_error():
    BACKOFF["topn"] = max(15, BACKOFF["topn"]-5)
    BACKOFF["scan_interval"] = min(90, BACKOFF["scan_interval"]+15)
    BACKOFF["sleep_ticker"] *= 1.5
    BACKOFF["sleep_ohlcv"] *= 1.5
    send_telegram(f"🧯 백오프 적용: TOPN={BACKOFF['topn']}, scan={BACKOFF['scan_interval']}s")

# ================= Scanner =================
TICKER_URL = "https://api.upbit.com/v1/ticker"
_last_scan_summary_ts = 0.0  # 10분 간격 스캔 요약

def fetch_top_by_turnover(krw_tickers, topn):
    res = []
    try:
        for i in range(0, len(krw_tickers), REQ_CHUNK):
            chunk = krw_tickers[i:i+REQ_CHUNK]
            r = requests.get(TICKER_URL, params={"markets": ",".join(chunk)}, timeout=5)
            if r.status_code == 429: on_rate_error(); continue
            r.raise_for_status()
            data = r.json()
            for d in data:
                res.append({"market": d["market"],
                            "price": float(d["trade_price"]),
                            "turnover24h": float(d.get("acc_trade_price_24h", 0.0))})
            time.sleep(BACKOFF["sleep_ticker"])
        res.sort(key=lambda x: x["turnover24h"], reverse=True)
        return res[:topn]
    except Exception as e:
        send_telegram(f"⚠️ 거래대금 조회 실패: {e}")
        return []

def scan_once_and_maybe_buy():
    global _last_scan_summary_ts

    # ★ 09:00 변동성 보호: 09시부터 NO_TRADE_MIN_AROUND_9 분 동안 신규진입 스킵
    kst = now_kst()
    if kst.hour == 9 and kst.minute < NO_TRADE_MIN_AROUND_9:
        if time.time() - _last_scan_summary_ts > 600:
            _last_scan_summary_ts = time.time()
            send_telegram("⏸ 09:00 변동성 보호로 신규 진입 스킵")
        return

    # 현재 보유/빈 슬롯
    with POS_LOCK:
        open_cnt = sum(1 for t,p in POS.items() if p.get("qty",0.0) > 0.0)
    free_slots = max(0, MAX_OPEN_POSITIONS - open_cnt)
    if free_slots <= 0:
        # 10분마다 요약
        if time.time() - _last_scan_summary_ts > 600:
            _last_scan_summary_ts = time.time()
            send_telegram(f"🔎 스캔요약: 후보 0 / free_slots=0 (슬롯 가득)")
        return

    # 유니버스/쿨다운/보유 제외
    try:
        krw_tickers = [t for t in pyupbit.get_tickers("KRW") if t not in EXCLUDED_TICKERS]
    except Exception as e:
        send_telegram(f"⚠️ 티커 조회 실패: {e}")
        return

    now = time.time()
    filtered = []
    for t in krw_tickers:
        if COOLDOWN.get(t, 0.0) > now: continue
        with POS_LOCK:
            if t in POS and POS[t].get("qty",0.0) > 0.0: continue
        filtered.append(t)

    # TOPN by turnover
    topN = fetch_top_by_turnover(filtered, BACKOFF["topn"])
    if not topN:
        if time.time() - _last_scan_summary_ts > 600:
            _last_scan_summary_ts = time.time()
            send_telegram(f"🔎 스캔요약: 후보 0 / TOPN={BACKOFF['topn']}")
        return

    # Score candidates
    cands = []
    for it in topN:
        t = it["market"]
        price = it["price"]
        if price < MIN_PRICE_KRW: continue
        try:
            df = get_ohlcv_safe(t, count=max(LOOKBACK_MIN+2, 15))
            if df is None or len(df) < LOOKBACK_MIN+2: continue
            closes = df["close"].tolist()
            vols   = df["volume"].tolist()
            ref = closes[-(LOOKBACK_MIN+1)]
            last = closes[-1]
            pct_up = (last - ref) / ref * 100.0
            vals = [c*v for c,v in zip(closes,vols)]
            past_avg = sum(vals[-(LOOKBACK_MIN+1):-1]) / LOOKBACK_MIN
            recent = vals[-1]
            spike = (recent/past_avg) if past_avg>0 else 0.0
            if pct_up >= MIN_PCT_UP and spike >= VOL_SPIKE_MULT:
                score = pct_up*0.7 + min(spike,5.0)*6.0
                cands.append((t, score, last, it["turnover24h"]))
            time.sleep(BACKOFF["sleep_ohlcv"]*(0.8+0.4*random.random()))
        except Exception as e:
            print(f"[scan:{t}] {e}")

    # 자금 계산
    krw_total = get_balance_krw()
    target_used = krw_total * (1.0 - CASH_BUFFER_PCT)
    inuse = 0.0
    with POS_LOCK:
        for t, p in POS.items():
            if p.get("qty",0.0) > 0:
                pr = get_price_safe(t) or p["avg"]
                inuse += p["qty"]*pr
    krw_left = max(0.0, target_used - inuse)

    # 10분마다 스캔 요약
    if time.time() - _last_scan_summary_ts > 600:
        _last_scan_summary_ts = time.time()
        est_per_slot = (krw_left / free_slots) if free_slots>0 else 0.0
        send_telegram(f"🔎 스캔요약: 후보 {len(cands)} / TOPN={BACKOFF['topn']} / free_slots={free_slots} / per_slot≈₩{est_per_slot:,.0f}")

    if not cands: return

    if krw_left < MIN_ORDER_KRW:
        send_telegram(f"⏸ 매수 스킵: 남은 매수가능액 ₩{krw_left:,.0f} < 최소주문 ₩{MIN_ORDER_KRW:,.0f}")
        return
    per_slot = krw_left / free_slots
    if per_slot < MIN_ORDER_KRW:
        send_telegram(f"⏸ 매수 스킵: per_slot ₩{per_slot:,.0f} < 최소주문 ₩{MIN_ORDER_KRW:,.0f}")
        return

    # 진입
    cands.sort(key=lambda x: (x[1], x[3]), reverse=True)
    picks = cands[:free_slots]
    for (t,score,last,turn) in picks:
        avg, qty = buy_market_krw(t, per_slot)
        if avg and qty and qty>0:
            with POS_LOCK:
                POS[t] = {
                    "qty": float(qty),
                    "avg": float(avg),
                    "entry_ts": now_str(),
                    "highest": float(avg),
                    "trail_active": False,
                    "partial_tp_done": False,
                    "cooldown_until": 0.0,
                }
            save_pos()
            tg_buy(t, qty, avg, per_slot)
            append_csv({"ts": now_str(),"ticker": t,"side":"BUY","qty": qty,"price": avg,
                        "krw": -(per_slot),"fee": per_slot*FEE_RATE,"pnl_krw":0,"pnl_pct":0,"note":"scanner_entry"})

# ================= Manager (TP/SL/Trailing/Partial) =================
def manage_positions_once():
    with POS_LOCK:
        items = list(POS.items())

    for t, p in items:
        qty = p.get("qty",0.0)
        if qty <= 0: continue
        avg = p.get("avg",0.0)
        price = get_price_safe(t)
        if not price or avg<=0: continue

        # ===== ★ 비상 하드스톱: 손익률 ≤ -HARD_STOP_PCT 즉시 청산 =====
        pnl_pct_now = (price-avg)/avg*100.0
        if pnl_pct_now <= -HARD_STOP_PCT:
            avg_sell, filled, got = sell_market_all(t)
            if filled and filled>0:
                pnl_pct = ((avg_sell-avg)/avg)*100.0 if avg_sell else pnl_pct_now
                tg_stop(t, avg_sell, filled, pnl_pct)
                append_csv({"ts": now_str(),"ticker": t,"side":"EMERGENCY_STOP","qty": filled,"price": avg_sell,
                            "krw": got,"fee": got*FEE_RATE,"pnl_krw": got - filled*avg,"pnl_pct": pnl_pct,"note": f"hard_stop{-HARD_STOP_PCT:.1f}%"})
                with POS_LOCK:
                    POS[t]["qty"] = 0.0
                    POS[t]["cooldown_until"] = time.time() + 5400  # 90분
                    COOLDOWN[t] = POS[t]["cooldown_until"]
                save_pos()
            continue
        # ============================================================

        highest = max(p.get("highest",avg), price)
        trail_active = p.get("trail_active", False)
        partial_done = p.get("partial_tp_done", False)

        # 트레일 활성화(+TRAIL_ACTIVATE_PCT %)
        if (not trail_active) and ((price-avg)/avg*100.0 >= TRAIL_ACTIVATE_PCT):
            trail_active = True
            send_telegram(f"🛡️ 트레일 활성화: {t} peak={highest:.4f} | line {TRAIL_PCT:.2f}%")

        sl_price = avg*(1 - SL_PCT/100.0)
        trail_line = highest*(1 - TRAIL_PCT/100.0) if trail_active else sl_price
        dyn_sl = max(sl_price, trail_line) if trail_active else sl_price

        # 1) 하드 스톱/트레일 라인(전량 청산)
        if price <= dyn_sl and pnl_pct_now < TP_PCT:
            avg_sell, filled, got = sell_market_all(t)
            if filled and filled>0:
                pnl_pct = ((avg_sell-avg)/avg)*100.0 if avg_sell else 0.0
                if pnl_pct < 0:
                    tg_stop(t, avg_sell, filled, pnl_pct)
                    end_reason = "손절"
                    cd_min = 90 if not p.get("partial_tp_done") else 30
                else:
                    tg_trailing(t, avg_sell, filled, pnl_pct)
                    end_reason = "트레일링"
                    cd_min = 30 if p.get("partial_tp_done") else 90

                append_csv({"ts": now_str(),"ticker": t,"side":"STOP/TRAIL","qty": filled,"price": avg_sell,
                            "krw": got,"fee": got*FEE_RATE,"pnl_krw": got - filled*avg,"pnl_pct": pnl_pct,"note": "dyn_sl"})
                with POS_LOCK:
                    POS[t]["qty"] = 0.0
                    POS[t]["cooldown_until"] = time.time() + (cd_min * 60)
                    COOLDOWN[t] = POS[t]["cooldown_until"]
                save_pos()
                tg_cooldown(t, end_reason if p.get("partial_tp_done") else f"{end_reason}/부분익절 미발생", cd_min)
            continue

        # 2) 부분익절 50% (1회)
        if (not partial_done) and (pnl_pct_now >= TP_PCT):
            avg_sell, filled, got = sell_market_ratio(t, PARTIAL_TP_RATIO)
            if filled and filled>0:
                pnl_pct = ((avg_sell-avg)/avg)*100.0 if avg_sell else TP_PCT
                left_after = max(0.0, (p.get("qty",0.0) or 0.0) - (filled or 0.0))
                tg_partial(t, filled, avg_sell, got - filled*avg, pnl_pct, left_after)

                append_csv({"ts": now_str(),"ticker": t,"side":"PARTIAL_TP","qty": filled,"price": avg_sell,
                            "krw": got,"fee": got*FEE_RATE,"pnl_krw": got - filled*avg,"pnl_pct": pnl_pct,"note":"partial@2.5%"})
                with POS_LOCK:
                    left = max(0.0, POS[t]["qty"] - filled)
                    POS[t]["qty"] = left
                    POS[t]["partial_tp_done"] = True
                    POS[t]["highest"] = max(POS[t]["highest"], price)
                    POS[t]["trail_active"] = True
                save_pos()
                continue

        # 3) 트레일 라인 터치로 잔량 전량 청산
        if trail_active and price <= (highest*(1 - TRAIL_PCT/100.0)):
            avg_sell, filled, got = sell_market_all(t)
            if filled and filled>0:
                pnl_pct = ((avg_sell-avg)/avg)*100.0 if avg_sell else 0.0
                tg_trailing(t, avg_sell, filled, pnl_pct)
                append_csv({"ts": now_str(),"ticker": t,"side":"TRAIL","qty": filled,"price": avg_sell,
                            "krw": got,"fee": got*FEE_RATE,"pnl_krw": got - filled*avg,"pnl_pct": pnl_pct,"note":"trail_hit"})
                with POS_LOCK:
                    POS[t]["qty"] = 0.0
                    POS[t]["cooldown_until"] = time.time() + (1800 if p.get("partial_tp_done") else 5400)
                    COOLDOWN[t] = POS[t]["cooldown_until"]
                save_pos()
                tg_cooldown(t, "트레일링", 30 if p.get("partial_tp_done") else 90)
                continue

        # 상태 저장(고빈도 저장 방지: 메모리만 갱신)
        with POS_LOCK:
            POS[t]["highest"] = highest
            POS[t]["trail_active"] = trail_active

# ================= Reporter (09:00:15 KST) =================
def tz_now():
    try:
        import zoneinfo
        return datetime.now(zoneinfo.ZoneInfo(REPORT_TZ))
    except Exception:
        return datetime.now()

def get_exchange_avg_map():
    out = {}
    try:
        bals = UPBIT.get_balances()
        for b in bals or []:
            cur = b.get("currency")
            if not cur: continue
            m = "KRW-"+cur.upper()
            qty = float(b.get("balance") or 0.0)
            if qty <= 0: continue
            avg = float(b.get("avg_buy_price") or 0.0)
            out[m] = avg if avg>0 else None
    except Exception as e:
        print(f"[avg_map] {e}")
    return out

def read_csv_rows():
    if not os.path.exists(CSV_FILE): return []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def summarize_between(start_dt_kst, end_dt_kst):
    rows = read_csv_rows()
    s = start_dt_kst.astimezone(KST)
    e = end_dt_kst.astimezone(KST)
    realized = 0.0; cnt=0; wins=0; losses=0; pnl_pct_sum=0.0
    for r in rows:
        ts = r.get("ts") or r.get("시간") or ""
        try:
            dt = datetime.fromisoformat(ts.replace(" ", "T"))
        except Exception:
            continue
        dt = dt.replace(tzinfo=KST)
        if not (s <= dt <= e): continue
        side = (r.get("side") or r.get("구분(익절/손절)") or "")
        if side:
            cnt += 1
            if "익절" in side or "TRAIL" in side or "PARTIAL" in side: wins += 1
            if "손절" in side or "STOP" in side: losses += 1
        realized += float(str(r.get("pnl_krw","0")).replace(",",""))
        pnl_pct_sum += float(str(r.get("pnl_pct","0")).replace(",",""))
    avg_pct = (pnl_pct_sum/cnt) if cnt else 0.0
    winrate = (wins/cnt*100.0) if cnt else 0.0
    return {"count":cnt,"wins":wins,"losses":losses,"realized_krw":realized,"avg_pnl_pct":avg_pct,"winrate":winrate}

def build_daily_report():
    now = tz_now()
    today_9 = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now >= today_9:
        start = today_9 - timedelta(days=1)
        end   = today_9 - timedelta(microseconds=1)
    else:
        start = today_9 - timedelta(days=2)
        end   = today_9 - timedelta(days=1, microseconds=1)
    met = summarize_between(start, end)

    with POS_LOCK:
        holdings = list(POS.items())
    avg_map = get_exchange_avg_map()

    lines = []
    total_hold_val = 0.0
    for t, p in holdings:
        qty = p.get("qty",0.0)
        if qty<=0: continue
        price = get_price_safe(t) or 0.0
        if price<=0: continue
        val = qty*price
        if val < DUST_KRW:
            lines.append(f"• {t} (dust) qty {qty:.6f}")
            continue
        avg = avg_map.get(t) or p.get("avg",0.0)
        upct = ((price-avg)/avg*100.0) if avg>0 else 0.0
        total_hold_val += val
        lines.append(f"• {t} qty {qty:.6f} @avg {avg:.4f} / now {price:.4f} → {upct:.2f}%")

    krw = get_balance_krw()
    total_assets = krw + total_hold_val

    msg = (
        f"📊 [일일 리포트] {now.strftime('%Y-%m-%d %H:%M')} ({REPORT_TZ})\n"
        f"집계 구간: {start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%Y-%m-%d %H:%M')} KST\n\n"
        f"🔹 실현손익: ₩{met['realized_krw']:.0f} | 거래수 {met['count']} (승 {met['wins']}/패 {met['losses']} | 승률 {met['winrate']:.1f}%) | 평균손익률 {met['avg_pnl_pct']:.2f}%\n\n"
        f"🔹 보유자산\n"
        + ("\n".join(lines) if lines else "• (없음)") + "\n\n"
        f"💼 보유금액(코인) ₩{total_hold_val:,.0f} | 현금 ₩{krw:,.0f} | 총자산 ₩{total_assets:,.0f}"
    )
    return msg

def get_last_report_date():
    try:
        with open(REPORT_SENT_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None

def set_last_report_date(s: str):
    try:
        with open(REPORT_SENT_FILE, "w", encoding="utf-8") as f:
            f.write(s)
    except Exception:
        pass

def reporter_loop():
    send_telegram("⏰ 리포터 시작")
    while True:
        try:
            now = tz_now()
            sched = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=15, microsecond=0)
            last = get_last_report_date()
            today = now.date().isoformat()
            if now >= sched and last != today:
                send_telegram(build_daily_report())
                set_last_report_date(today)
                time.sleep(90)
                continue
            time.sleep(30)
        except Exception:
            print(f"[reporter] {traceback.format_exc()]}")
            time.sleep(5)

# ================= Loops =================
def scanner_loop():
    send_telegram(f"🔎 스캐너 시작 (24h, TOPN={BACKOFF['topn']})")
    while True:
        try:
            scan_once_and_maybe_buy()
        except Exception:
            print(f"[scanner] {traceback.format_exc()}")
            on_rate_error()
        time.sleep(BACKOFF["scan_interval"])

def manager_loop():
    send_telegram("🧭 매니저 시작 (SL/Partial/Trailing)")
    while True:
        try:
            manage_positions_once()
        except Exception:
            print(f"[manager] {traceback.format_exc()}")
        # ★ 매니저 주기: 기본 0.4초 (ENV로 조절)
        time.sleep(max(0.1, MANAGER_TICK_MS/1000.0))

# ================= Boot =================
def init_bot():
    if not ACCESS_KEY or not SECRET_KEY:
        raise RuntimeError("ACCESS_KEY/SECRET_KEY not set")
    # Diag
    try:
        payload = {'access_key': ACCESS_KEY, 'nonce': str(uuid.uuid4())}
        token = jwt.encode(payload, SECRET_KEY, algorithm='HS256')
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get("https://api.upbit.com/v1/accounts", headers=headers, timeout=10)
        if r.status_code != 200:
            send_telegram(f"❗️업비트 인증/허용IP/레이트리밋 점검: {r.status_code}")
    except Exception as e:
        print(f"[diag] {e}")
    load_pos()
    send_telegram("🤖 봇 시작됨")
    # 시작 시 현재 임계치 브리핑(가시성)
    send_telegram(
        f"⚙️ thresholds | 10m≥+{MIN_PCT_UP:.1f}% & 1m×≥{VOL_SPIKE_MULT:.2f} | "
        f"SL={SL_PCT:.2f}% | TP(partial@{TP_PCT:.1f}%, {int(PARTIAL_TP_RATIO*100)}%) | "
        f"TRAIL act {TRAIL_ACTIVATE_PCT:.1f}% / line {TRAIL_PCT:.1f}% | "
        f"hard_stop {HARD_STOP_PCT:.1f}% | manager {MANAGER_TICK_MS}ms | TOPN={BACKOFF['topn']}"
    )

def start_threads():
    threading.Thread(target=scanner_loop, daemon=True).start()
    threading.Thread(target=manager_loop, daemon=True).start()
    threading.Thread(target=reporter_loop, daemon=True).start()

# === import-time autostart for gunicorn/wsgi ===
if not getattr(app, "_bot_started", False):
    init_bot()
    start_threads()
    app._bot_started = True

# ================= __main__ =================
if __name__ == "__main__":
    # (gunicorn에서도 이미 autostart 되었지만, 단독 실행시도 대응)
    if not getattr(app, "_bot_started", False):
        init_bot(); start_threads(); app._bot_started = True
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

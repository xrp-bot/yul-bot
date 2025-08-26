# main.py — Upbit Bottom-Entry Bot (Cash-Only Budget + Reserved Pool + SafeOrders + Exact PnL + Trail Alerts)
# 2025-08 final
# - 스캐너: TOPN 상위 유니버스 + 바닥반등(완화) 조건
# - 예산: "현금"×(1-버퍼)만 사용, 슬롯 균등. 최소주문 미만이면 자동 합산하여 1건으로 집행 + 잔액은 RESERVED_POOL로 이월
# - 매도: 손절 -1.2% → 부분익절 50%@+2.5%(1회) → 트레일링(최고가-1.5%) / 비상 하드스톱 -2.5% / 프리-스톱 -0.9%
# - 체결가 기반 PnL: 매수/매도 모두 실체결가로 손익 계산/로그
# - 트레일링: 활성화 알림 1회 보장(trail_alerted 플래그), highest 선 갱신
# - Dust: 평가금액 < DUST_LIMIT_KRW는 청소/숨김
# - 09:00:15 KST 일일 리포트 + Dust 청소
# - Render/Gunicorn 호환: import-time autostart

import os, time, json, csv, math, random, requests, threading, traceback, uuid, jwt, pyupbit
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify

# ===================== Flask =====================
app = Flask(__name__)

@app.get("/")
def home():
    return "✅ Upbit Bottom-Entry Bot running"

@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": datetime.now().isoformat()}), 200

@app.get("/portfolio")
def portfolio():
    with POS_LOCK:
        snap = {t: POS[t].copy() for t in POS}
    prices = {t: get_price_safe(t) for t in snap}
    return jsonify({"ok": True, "positions": snap, "prices": prices}), 200

@app.get("/reconcile")
def reconcile():
    try:
        avg_map = get_exchange_avg_map()
        removed = 0
        with POS_LOCK:
            for t in list(POS.keys()):
                sym = t.split("-")[1]
                qty_ex = get_balance_coin(sym)
                price = get_price_safe(t) or POS[t].get("avg", 0.0)
                est = qty_ex * (price or 0.0)
                if est < DUST_LIMIT_KRW:
                    POS[t]["qty"] = 0.0
                    removed += 1
                else:
                    POS[t]["qty"] = qty_ex
                    POS[t]["avg"] = avg_map.get(t, POS[t].get("avg", 0.0)) or POS[t].get("avg", 0.0)
        save_pos()
        send_telegram(f"♻️ 재동기화 완료 — dust 제거 {removed}건")
        return {"ok": True, "dust_removed": removed}, 200
    except Exception as e:
        return {"ok": False, "err": str(e)}, 500

# ===================== ENV =====================
ACCESS_KEY       = os.getenv("ACCESS_KEY")
SECRET_KEY       = os.getenv("SECRET_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 예산/리스크
CASH_BUFFER_PCT        = float(os.getenv("CASH_BUFFER_PCT", "0.10"))  # 현금 10% 버퍼
MAX_OPEN_POSITIONS     = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
MIN_ORDER_KRW          = float(os.getenv("MIN_ORDER_KRW", "5000"))
FEE_RATE               = float(os.getenv("FEE_RATE", "0.0005"))
DUST_LIMIT_KRW         = float(os.getenv("DUST_LIMIT_KRW", "2000"))

# 스캐너
SCAN_INTERVAL_SEC      = int(os.getenv("SCAN_INTERVAL_SEC", "45"))
TOPN_INITIAL           = int(os.getenv("TOPN_INITIAL", "25"))
MIN_PRICE_KRW          = float(os.getenv("MIN_PRICE_KRW", "100"))
EXCLUDED_TICKERS       = set([t.strip() for t in os.getenv("EXCLUDED_TICKERS","KRW-BTC,KRW-ETH").split(",") if t.strip()])

# 바닥 진입(완화 프리셋)
RSI_MAX_BOTTOM         = float(os.getenv("RSI_MAX_BOTTOM", "45"))
EMA_NEAR_PCT           = float(os.getenv("EMA_NEAR_PCT", "2.0"))
REBOUND_FROM_LOW_PCT   = float(os.getenv("REBOUND_FROM_LOW_PCT", "0.5"))
VOL_BOOST_MULT         = float(os.getenv("VOL_BOOST_MULT", "1.2"))
LOOKBACK_MIN           = int(os.getenv("LOOKBACK_MIN", "10"))

# 매도/리스크
SL_PCT                 = float(os.getenv("SL_PCT", "1.2"))    # 기본 스탑로스
TP_PCT                 = float(os.getenv("TP_PCT", "2.5"))    # 부분익절 트리거
TRAIL_ACTIVATE_PCT     = float(os.getenv("TRAIL_ACTIVATE_PCT", "1.0"))
TRAIL_PCT              = float(os.getenv("TRAIL_PCT", "1.5"))
PARTIAL_TP_RATIO       = float(os.getenv("PARTIAL_TP_RATIO", "0.5"))
HARD_STOP_PCT          = float(os.getenv("HARD_STOP_PCT", "2.5"))
PRESTOP_PCT            = float(os.getenv("PRESTOP_PCT", "0.9"))  # 프리-스톱(선제 청산)

# 리포트
REPORT_TZ              = os.getenv("REPORT_TZ", "Asia/Seoul")
REPORT_HOUR            = int(os.getenv("REPORT_HOUR", "9"))
REPORT_MINUTE          = int(os.getenv("REPORT_MINUTE", "0"))
REPORT_SENT_FILE       = os.getenv("REPORT_SENT_FILE", "./last_report_date.txt")

# 운영
NO_TRADE_MIN_AROUND_9  = int(os.getenv("NO_TRADE_MIN_AROUND_9", "3"))
PERSIST_DIR            = os.getenv("PERSIST_DIR", "./")
os.makedirs(PERSIST_DIR, exist_ok=True)
CSV_FILE               = os.path.join(PERSIST_DIR, "trades.csv")
POS_FILE               = os.path.join(PERSIST_DIR, "pos.json")

# ===================== Globals =====================
KST = timezone(timedelta(hours=9))
UPBIT = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY) if (ACCESS_KEY and SECRET_KEY) else None

POS_LOCK = threading.Lock()
POS: dict[str, dict] = {}
COOLDOWN: dict[str, float] = {}
BACKOFF = {"topn": TOPN_INITIAL, "scan_interval": SCAN_INTERVAL_SEC}
RESERVED_POOL = 0.0   # under-min 잔액 누적 풀

# ===================== Telegram =====================
def _post_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=5)
    except Exception as e:
        print(f"[TG_FAIL] {e} | {text}")

def send_telegram(msg: str): _post_telegram(msg)

# ===================== Utilities =====================
def now_kst() -> datetime: return datetime.now(tz=KST)
def now_str() -> str: return now_kst().strftime("%Y-%m-%d %H:%M:%S")

def get_price_safe(ticker, tries=3, delay=0.6):
    for i in range(tries):
        try:
            p = pyupbit.get_current_price(ticker)
            if p: return float(p)
        except Exception as e:
            print(f"[price:{ticker}] {e}")
        time.sleep(delay*(i+1))
    return None

def get_ohlcv_safe(ticker, count=60, interval="minute1", tries=3, delay=0.6):
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
            qty = float(p.get("qty", 0.0))
            if qty <= 0: continue
            price = get_price_safe(t) or p.get("avg", 0.0)
            if qty*(price or 0.0) < DUST_LIMIT_KRW: continue
            obj[t] = {
                "qty": qty, "avg": float(p.get("avg", 0.0)),
                "entry_ts": p.get("entry_ts"), "highest": float(p.get("highest", 0.0)),
                "trail_active": bool(p.get("trail_active", False)),
                "partial_tp_done": bool(p.get("partial_tp_done", False)),
                "cooldown_until": float(p.get("cooldown_until", 0.0)),
                "trail_alerted": bool(p.get("trail_alerted", False)),
                "trail_last_alert_price": float(p.get("trail_last_alert_price", 0.0)),
            }
    tmp = POS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False, indent=2)
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
                "qty": float(p.get("qty", 0.0)),
                "avg": float(p.get("avg", 0.0)),
                "entry_ts": p.get("entry_ts"),
                "highest": float(p.get("highest", 0.0)),
                "trail_active": bool(p.get("trail_active", False)),
                "partial_tp_done": bool(p.get("partial_tp_done", False)),
                "cooldown_until": float(p.get("cooldown_until", 0.0)),
                "trail_alerted": bool(p.get("trail_alerted", False)),
                "trail_last_alert_price": float(p.get("trail_last_alert_price", 0.0)),
            }

def append_csv(row: dict):
    header = ["ts","ticker","side","qty","price","krw","fee","pnl_krw","pnl_pct","note"]
    exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if not exists: w.writeheader()
        w.writerow(row)

# ===================== Exchange helpers =====================
def get_balance_krw():
    try: return float(UPBIT.get_balance("KRW") or 0.0)
    except Exception: return 0.0

def get_balance_coin(symbol_without_prefix):
    try: return float(UPBIT.get_balance(symbol_without_prefix) or 0.0)
    except Exception: return 0.0

def get_exchange_avg_map():
    out = {}
    try:
        for b in UPBIT.get_balances() or []:
            cur = b.get("currency"); qty = float(b.get("balance") or 0.0)
            if not cur or qty <= 0: continue
            out["KRW-"+cur.upper()] = float(b.get("avg_buy_price") or 0.0) or None
    except Exception as e:
        print(f"[avg_map] {e}")
    return out

# ===================== SafeOrders (exact fill PnL) =====================
_last_order_at = {}
def _rate_gate(sym, cooldown=0.8):
    last = _last_order_at.get(sym, 0.0)
    wait = cooldown - max(0.0, time.time() - last)
    if wait > 0: time.sleep(wait)

def safe_buy_market(market: str, krw_amount: float):
    if krw_amount < MIN_ORDER_KRW:
        return {"status":"SKIP","reason":"under_min_order"}
    sym = market.split("-")[1].upper()
    _rate_gate(sym)
    krw_before = get_balance_krw()
    coin_before = get_balance_coin(sym)
    resp = None
    for _ in range(5):
        try: resp = UPBIT.buy_market_order(market, krw_amount*0.9990)
        except Exception: resp = None
        _last_order_at[sym] = time.time()
        if resp: break
        time.sleep(0.6)
    if not resp: return {"status":"FAIL","reason":"resp_none"}
    t0 = time.time()
    while time.time()-t0 < 30:
        if get_balance_coin(sym) > coin_before + 1e-9: break
        time.sleep(0.5)
    coin_after = get_balance_coin(sym)
    qty = max(0.0, coin_after - coin_before)
    krw_after = get_balance_krw()
    spent = max(0.0, krw_before - krw_after)
    avg = spent/qty if qty>0 else (get_price_safe(market) or 0.0)
    return {"status":"OK","avg":avg,"qty":qty,"spent":spent}

def safe_sell_market(market: str, portion: float = 1.0):
    sym = market.split("-")[1].upper()
    bal_before = get_balance_coin(sym)
    if bal_before <= 0: return {"status":"EMPTY"}
    price_now = get_price_safe(market) or 0.0
    qty = math.floor(bal_before*portion*10**6)/10**6
    if qty*price_now < MIN_ORDER_KRW:
        est_all = bal_before*price_now
        if est_all < DUST_LIMIT_KRW:
            try:
                _rate_gate(sym); resp = UPBIT.sell_market_order(market, bal_before)
                _last_order_at[sym] = time.time()
                # 아래에서 fill 계산
            except Exception:
                return {"status":"DUST_SKIP"}
        else:
            return {"status":"SKIP","reason":"under_min_order"}
    else:
        _rate_gate(sym)
        resp = None
        for _ in range(5):
            try: resp = UPBIT.sell_market_order(market, qty)
            except Exception: resp = None
            _last_order_at[sym] = time.time()
            if resp: break
            time.sleep(0.5)
        if not resp: return {"status":"FAIL","reason":"resp_none"}

    # fill 측정 (정확한 avg_sell)
    krw_b = get_balance_krw()
    bal_mid = get_balance_coin(sym)  # 호출 타이밍 보정용
    time.sleep(0.6)
    krw_a = get_balance_krw()
    bal_a = get_balance_coin(sym)
    filled = max(0.0, bal_before - bal_a)
    received = max(0.0, krw_a - krw_b)
    if filled <= 0:  # 체결 대기 루프
        t0 = time.time()
        while time.time()-t0 < 30:
            bal_a = get_balance_coin(sym)
            krw_a = get_balance_krw()
            filled = max(0.0, bal_before - bal_a)
            received = max(0.0, krw_a - krw_b)
            if filled > 0: break
            time.sleep(0.5)
    avg_sell = (received/filled) if filled>0 else (get_price_safe(market) or price_now)
    return {"status":"OK","filled":filled,"received":received,"avg_sell":avg_sell}

# ===================== Indicators =====================
def ema(series, span):
    import pandas as pd
    return pd.Series(series).ewm(span=span, adjust=False).mean().tolist()

def rsi14(df):
    import pandas as pd
    d = df["close"].diff()
    up = d.clip(lower=0); down = -d.clip(upper=0)
    ru = up.ewm(span=14, adjust=False).mean()
    rd = down.ewm(span=14, adjust=False).mean()
    rs = ru/(rd+1e-12)
    return (100 - (100/(1+rs))).tolist()

# ===================== Scanner =====================
TICKER_URL = "https://api.upbit.com/v1/ticker"
_last_summary_ts = 0.0

def fetch_top_by_turnover(krw_tickers, topn):
    res = []
    try:
        CHUNK = 90
        for i in range(0, len(krw_tickers), CHUNK):
            chunk = krw_tickers[i:i+CHUNK]
            r = requests.get(TICKER_URL, params={"markets":",".join(chunk)}, timeout=5)
            if r.status_code == 429:
                BACKOFF["topn"] = max(15, BACKOFF["topn"]-5)
                BACKOFF["scan_interval"] = min(90, BACKOFF["scan_interval"]+15)
                continue
            r.raise_for_status()
            for d in r.json():
                res.append({"market":d["market"],"price":float(d["trade_price"]),
                            "turnover24h":float(d.get("acc_trade_price_24h",0.0))})
            time.sleep(0.08)
        res.sort(key=lambda x: x["turnover24h"], reverse=True)
        return res[:topn]
    except Exception as e:
        send_telegram(f"⚠️ 거래대금 조회 실패: {e}")
        return []

def _summary(cand_cnt, slots_left, per_slot, stats=None):
    global _last_summary_ts
    if time.time() - _last_summary_ts < 600: return
    _last_summary_ts = time.time()
    base = f"🔎 스캔요약: 후보 {cand_cnt} / TOPN={BACKOFF['topn']} / slots_left={slots_left} / per_slot≈₩{per_slot:,.0f}"
    if stats:
        base += f"\nscan={stats['scanned']} | ok={stats['ok']} | fail rsi={stats['rsi_fail']}, ema={stats['ema_fail']}, rebound={stats['rebound_fail']}, vol={stats['vol_fail']}"
    send_telegram(base)

def scan_once_and_maybe_buy():
    global RESERVED_POOL

    # 09:00 변동성 보호
    k = now_kst()
    if k.hour == 9 and k.minute < NO_TRADE_MIN_AROUND_9:
        return

    # 슬롯
    with POS_LOCK:
        open_cnt = sum(1 for _, p in POS.items() if p.get("qty",0.0) > 0)
    slots_left = max(0, MAX_OPEN_POSITIONS - open_cnt)
    if slots_left == 0: return

    # 유니버스
    try:
        all_tk = [t for t in pyupbit.get_tickers("KRW") if t not in EXCLUDED_TICKERS]
    except Exception as e:
        send_telegram(f"⚠️ 티커 조회 실패: {e}"); return

    now_ep = time.time()
    uni = []
    for t in all_tk:
        if COOLDOWN.get(t, 0.0) > now_ep: continue
        with POS_LOCK:
            if t in POS and POS[t].get("qty",0.0) > 0: continue
        uni.append(t)

    topN = fetch_top_by_turnover(uni, BACKOFF["topn"])
    if not topN:
        # 후보 없어도 RESERVED_POOL은 유지
        _summary(0, slots_left, 0.0)
        return

    # 후보 평가 (바닥 반등)
    cands = []
    stats = {"scanned":0,"rsi_fail":0,"ema_fail":0,"rebound_fail":0,"vol_fail":0,"ok":0}
    for it in topN:
        t = it["market"]; px = it["price"]
        if px < MIN_PRICE_KRW: continue
        df = get_ohlcv_safe(t, count=max(LOOKBACK_MIN+25, 50))
        if df is None or len(df) < LOOKBACK_MIN+5: continue

        closes = df["close"].tolist()
        highs  = df["high"].tolist()
        lows   = df["low"].tolist()
        vols   = df["volume"].tolist()
        stats["scanned"] += 1

        rsi = rsi14(df); rsi_ok = (rsi[-1] if isinstance(rsi[-1], (int,float)) else 50.0) <= RSI_MAX_BOTTOM
        if not rsi_ok: stats["rsi_fail"] += 1

        ema10 = ema(closes, 10)[-1]; ema20 = ema(closes, 20)[-1]
        last = closes[-1]
        ema_ok = (abs(last-ema10)/max(1e-9, ema10) <= EMA_NEAR_PCT/100.0) or \
                 (abs(last-ema20)/max(1e-9, ema20) <= EMA_NEAR_PCT/100.0)
        if not ema_ok: stats["ema_fail"] += 1

        recent_low = min(lows[-(LOOKBACK_MIN//2+5):])
        rebound_ok = (last >= recent_low*(1+REBOUND_FROM_LOW_PCT/100.0))
        if not rebound_ok: stats["rebound_fail"] += 1

        v10 = sum(vols[-11:-1])/10.0 if len(vols) >= 11 else sum(vols)/max(1,len(vols))
        vol_ok = (vols[-1] >= v10*VOL_BOOST_MULT)
        if not vol_ok: stats["vol_fail"] += 1

        if rsi_ok and ema_ok and rebound_ok and vol_ok:
            score = (50-(rsi[-1] if isinstance(rsi[-1], (int,float)) else 50)) + (vols[-1]/(v10+1e-9)) + (last/recent_low)
            cands.append((t, score, last, it["turnover24h"]))
            stats["ok"] += 1
        time.sleep(0.03+0.02*random.random())

    # ===== 현금만 기준 + under-min 누적 풀 =====
    krw_cash = get_balance_krw()
    usable = krw_cash * (1.0 - CASH_BUFFER_PCT) + RESERVED_POOL

    if usable < MIN_ORDER_KRW:
        RESERVED_POOL = max(0.0, usable)
        _summary(len(cands), slots_left, 0.0, stats)
        return

    per_slot = usable/slots_left if slots_left>0 else 0.0
    if per_slot < MIN_ORDER_KRW:
        slots_to_use = 1
        per_slot = usable  # 한 번에 몰아 매수
    else:
        slots_to_use = min(slots_left, int(usable // MIN_ORDER_KRW))
        if slots_to_use == 0:
            RESERVED_POOL = usable
            _summary(len(cands), slots_left, 0.0, stats)
            return

    _summary(len(cands), slots_to_use, per_slot, stats)

    if not cands:
        # 후보 없으면 usable을 풀에 적립하여 다음 스캔에 합산 사용
        RESERVED_POOL = usable
        return

    cands.sort(key=lambda x: (x[1], x[3]), reverse=True)
    picks = cands[:slots_to_use]

    spent_total = 0.0
    for (t,_,__,___) in picks:
        br = safe_buy_market(t, per_slot)
        if br.get("status")=="OK" and br.get("qty",0)>0:
            avg, qty, spent = br["avg"], br["qty"], br["spent"]
            with POS_LOCK:
                POS[t] = {
                    "qty": float(qty), "avg": float(avg),
                    "entry_ts": now_str(), "highest": float(avg),
                    "trail_active": False, "partial_tp_done": False,
                    "trail_alerted": False, "trail_last_alert_price": 0.0,
                    "cooldown_until": 0.0,
                }
            save_pos()
            spent_total += spent
            send_telegram(
                "🚀 매수 체결\n"
                f"— 심볼: {t}\n"
                f"— 수량: {qty:.6f}\n"
                f"— 체결가: ₩{avg:,.4f}\n"
                f"— 투자금액: ₩{spent:,.0f}\n"
                f"— 기준: 현금×(1-버퍼) 합산/슬롯 = ₩{per_slot:,.0f}"
            )
            append_csv({"ts": now_str(),"ticker": t,"side":"BUY","qty": qty,"price": avg,
                        "krw": -spent,"fee": spent*FEE_RATE,"pnl_krw":0,"pnl_pct":0,"note":"bottom_entry(cash_only+pool)"})

    RESERVED_POOL = max(0.0, usable - spent_total)

# ===================== Manager (TP/SL/Trail + PreStop) =====================
def manage_positions_once():
    with POS_LOCK:
        items = list(POS.items())

    for t, p in items:
        qty = p.get("qty",0.0)
        if qty <= 0: continue
        avg = p.get("avg",0.0)
        price = get_price_safe(t)
        if not price or avg<=0: continue

        pnl_pct_now = (price-avg)/avg*100.0

        # 최고가 선 갱신
        highest = max(p.get("highest",avg), price)
        trail_active = bool(p.get("trail_active", False))
        trail_alerted = bool(p.get("trail_alerted", False))
        partial_done = bool(p.get("partial_tp_done", False))

        # 프리-스톱(선제 청산)
        if pnl_pct_now <= -PRESTOP_PCT:
            sr = safe_sell_market(t, 1.0)
            if sr.get("status") == "OK":
                filled = sr.get("filled", qty)
                avg_sell = sr.get("avg_sell") or price
                pnl_pct = ((avg_sell-avg)/avg)*100.0
                _after_close(t, p, filled, avg_sell, pnl_pct, label="PRE-STOP")
                continue

        # 트레일 활성화 알림(1회 보장)
        if (not trail_active) and (pnl_pct_now >= TRAIL_ACTIVATE_PCT):
            trail_active = True
            trail_alerted = True
            send_telegram(
                "🛡️ 트레일 활성화\n"
                f"— 심볼: {t}\n"
                f"— 현재수익률: {pnl_pct_now:.2f}% (임계 {TRAIL_ACTIVATE_PCT:.2f}%)\n"
                f"— 최신 최고가: ₩{highest:,.4f}\n"
                f"— 라인: 최고가 - {TRAIL_PCT:.2f}%"
            )

        # 비상 하드스톱
        if pnl_pct_now <= -HARD_STOP_PCT:
            sr = safe_sell_market(t, 1.0)
            if sr.get("status") == "OK":
                filled = sr.get("filled", qty)
                avg_sell = sr.get("avg_sell") or price
                pnl_pct = ((avg_sell-avg)/avg)*100.0
                _after_close(t, p, filled, avg_sell, pnl_pct, label="EMERGENCY_STOP")
                continue

        # 동적 손절/트레일 라인
        sl_price = avg*(1 - SL_PCT/100.0)
        trail_line = highest*(1 - TRAIL_PCT/100.0) if trail_active else sl_price
        dyn_sl = max(sl_price, trail_line) if trail_active else sl_price

        if price <= dyn_sl and pnl_pct_now < TP_PCT:
            sr = safe_sell_market(t, 1.0)
            if sr.get("status") == "OK":
                filled = sr.get("filled", qty)
                avg_sell = sr.get("avg_sell") or price
                pnl_pct = ((avg_sell-avg)/avg)*100.0
                _after_close(t, p, filled, avg_sell, pnl_pct, label="STOP/TRAIL")
                continue

        # 부분익절 1회
        if (not partial_done) and (pnl_pct_now >= TP_PCT):
            sr = safe_sell_market(t, PARTIAL_TP_RATIO)
            if sr.get("status") in ("OK","DUST_CLEAN"):
                sold = sr.get("filled", qty*PARTIAL_TP_RATIO)
                avg_sell = sr.get("avg_sell") or price
                pnl_pct = ((avg_sell-avg)/avg)*100.0
                left = max(0.0, qty - sold)
                send_telegram(
                    "✅ 부분 익절 (50%)\n"
                    f"— 심볼: {t}\n"
                    f"— 수량: {sold:.6f}\n"
                    f"— 매도가: ₩{avg_sell:,.4f}\n"
                    f"— 실현손익률: {pnl_pct:.2f}%\n"
                    f"— 잔여: {left:.6f}"
                )
                append_csv({"ts": now_str(),"ticker": t,"side":"PARTIAL_TP","qty": sold,"price": avg_sell,
                            "krw": sold*avg_sell,"fee": sold*avg_sell*FEE_RATE,"pnl_krw": sold*(avg_sell-avg),
                            "pnl_pct": pnl_pct,"note":"partial@TP"})
                with POS_LOCK:
                    POS[t]["qty"] = left
                    POS[t]["partial_tp_done"] = True
                    POS[t]["highest"] = max(highest, price)
                    POS[t]["trail_active"] = True
                    POS[t]["trail_alerted"] = True
                save_pos()
                continue

        # 트레일 라인 터치로 잔량 청산
        if trail_active and price <= highest*(1 - TRAIL_PCT/100.0):
            sr = safe_sell_market(t, 1.0)
            if sr.get("status") == "OK":
                filled = sr.get("filled", qty)
                avg_sell = sr.get("avg_sell") or price
                pnl_pct = ((avg_sell-avg)/avg)*100.0
                _after_close(t, p, filled, avg_sell, pnl_pct, label="TRAIL")
                continue

        # 상태 저장
        with POS_LOCK:
            POS[t]["highest"] = highest
            POS[t]["trail_active"] = trail_active
            POS[t]["trail_alerted"] = trail_alerted
        # save_pos()는 이벤트 시점에서만 호출

def _after_close(ticker, pos, filled, avg_sell, pnl_pct, label):
    qty = pos.get("qty",0.0); avg = pos.get("avg",0.0)
    if pnl_pct < 0:
        send_telegram(
            "⚠️ 손절 매도\n"
            f"— 심볼: {ticker}\n— 수량: {qty:.6f}\n— 매도가: ₩{avg_sell:,.4f}\n— 손익률: {pnl_pct:.2f}%"
        )
    else:
        send_telegram(
            "📉 트레일링/청산\n"
            f"— 심볼: {ticker}\n— 수량: {qty:.6f}\n— 매도가: ₩{avg_sell:,.4f}\n— 손익률: {pnl_pct:.2f}%"
        )
    append_csv({"ts": now_str(),"ticker": ticker,"side": label,"qty": qty,"price": avg_sell,
                "krw": qty*avg_sell,"fee": qty*avg_sell*FEE_RATE,"pnl_krw": qty*(avg_sell-avg),
                "pnl_pct": pnl_pct,"note":"close_all"})
    with POS_LOCK:
        POS[ticker]["qty"] = 0.0
        POS[ticker]["cooldown_until"] = time.time() + (1800 if pos.get("partial_tp_done") else 5400)
        COOLDOWN[ticker] = POS[ticker]["cooldown_until"]
    save_pos()
    send_telegram(f"⏳ 쿨다운 적용 — {ticker} / {30 if pos.get('partial_tp_done') else 90}분")

# ===================== Reporter & Dust Cleaner =====================
def tz_now():
    try:
        import zoneinfo
        return datetime.now(zoneinfo.ZoneInfo(REPORT_TZ))
    except Exception:
        return datetime.now()

def _read_csv():
    if not os.path.exists(CSV_FILE): return []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def build_daily_report_and_clean_dust():
    now = tz_now()
    today_9 = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now >= today_9:
        start = today_9 - timedelta(days=1); end = today_9 - timedelta(microseconds=1)
    else:
        start = today_9 - timedelta(days=2); end = today_9 - timedelta(days=1, microseconds=1)

    rows = _read_csv()
    s = start.replace(tzinfo=KST); e = end.replace(tzinfo=KST)
    realized = 0.0; cnt=wins=losses=0; pnl_pct_sum=0.0
    for r in rows:
        ts = r.get("ts","")
        try: dt = datetime.fromisoformat(ts.replace(" ","T")).replace(tzinfo=KST)
        except Exception: continue
        if not (s <= dt <= e): continue
        side = r.get("side","")
        cnt += 1
        if "TRAIL" in side or "PARTIAL" in side: wins += 1
        if "STOP" in side or "EMERGENCY" in side or "PRE-STOP" in side: losses += 1
        realized += float(str(r.get("pnl_krw","0")).replace(",",""))
        pnl_pct_sum += float(str(r.get("pnl_pct","0")).replace(",",""))
    avg_pct = (pnl_pct_sum/cnt) if cnt else 0.0
    winrate = (wins/cnt*100.0) if cnt else 0.0

    # Dust 청소
    try:
        bals = UPBIT.get_balances()
        cleaned = 0
        for b in bals or []:
            cur = b.get("currency")
            if not cur or cur=="KRW": continue
            qty = float(b.get("balance") or 0.0)
            avg = float(b.get("avg_buy_price") or 0.0)
            if qty<=0: continue
            est = qty*(avg or 0.0)
            market = "KRW-"+cur.upper()
            if est < DUST_LIMIT_KRW:
                try: UPBIT.sell_market_order(market, qty)
                except Exception: pass
                with POS_LOCK:
                    if market in POS: POS[market]["qty"] = 0.0
                cleaned += 1
        if cleaned>0:
            save_pos()
            send_telegram(f"🧹 Dust 청산/제거 {cleaned}건 완료 (limit ₩{int(DUST_LIMIT_KRW)})")
    except Exception as e:
        send_telegram(f"⚠️ Dust 청산 중 오류: {e}")

    with POS_LOCK:
        holdings = [(t,p) for t,p in POS.items() if p.get("qty",0.0)>0]
    lines=[]; total_val=0.0
    for t,p in holdings:
        pr = get_price_safe(t) or 0.0
        if p["qty"]*pr < DUST_LIMIT_KRW:  # dust 숨김
            continue
        total_val += p["qty"]*pr
        lines.append(f"• {t} qty {p['qty']:.6f} @avg {p['avg']:.4f} / now {pr:.4f}")
    krw = get_balance_krw(); total_assets = krw + total_val

    return (
        f"📊 [일일 리포트] {now.strftime('%Y-%m-%d %H:%M')} ({REPORT_TZ})\n"
        f"거래수 {cnt} (승 {wins}/패 {losses} | 승률 {winrate:.1f}%) | 평균손익률 {avg_pct:.2f}%\n\n"
        f"🔹 보유자산\n" + ("\n".join(lines) if lines else "• (없음)") + "\n\n"
        f"💼 보유금액(코인) ₩{total_val:,.0f} | 현금 ₩{krw:,.0f} | 총자산 ₩{total_assets:,.0f}"
    )

def reporter_loop():
    send_telegram("⏰ 리포터 시작")
    while True:
        try:
            now = tz_now()
            sched = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=15, microsecond=0)
            last = None
            try:
                with open(REPORT_SENT_FILE,"r",encoding="utf-8") as f: last = f.read().strip()
            except Exception: pass
            today = now.date().isoformat()
            if now >= sched and last != today:
                msg = build_daily_report_and_clean_dust()
                send_telegram(msg)
                try:
                    with open(REPORT_SENT_FILE,"w",encoding="utf-8") as f: f.write(today)
                except Exception: pass
                time.sleep(90)
            time.sleep(30)
        except Exception:
            print(f"[reporter] {traceback.format_exc()}"); time.sleep(5)

# ===================== Loops =====================
def scanner_loop():
    send_telegram(f"🔎 스캐너 시작 (TOPN={BACKOFF['topn']})")
    while True:
        try: scan_once_and_maybe_buy()
        except Exception:
            print(f"[scanner] {traceback.format_exc()}")
        time.sleep(BACKOFF["scan_interval"])

def manager_loop():
    send_telegram("🧭 매니저 시작 (SL/Partial/Trailing)")
    while True:
        try: manage_positions_once()
        except Exception:
            print(f"[manager] {traceback.format_exc()}")
        # 환경변수로 150ms 권장 (Render Settings → MANAGER_TICK_MS=150)
        time.sleep(max(0.1, float(os.getenv("MANAGER_TICK_MS","150"))/1000.0))

# ===================== Boot =====================
def init_bot():
    if not ACCESS_KEY or not SECRET_KEY:
        raise RuntimeError("ACCESS_KEY/SECRET_KEY not set")
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
    send_telegram(
        f"⚙️ thresholds | RSI≤{RSI_MAX_BOTTOM} | EMA±{EMA_NEAR_PCT}% | "
        f"Rebound≥{REBOUND_FROM_LOW_PCT}% | Vol×≥{VOL_BOOST_MULT} | "
        f"SL={SL_PCT}% | TP@{TP_PCT}%/50% | Trail act {TRAIL_ACTIVATE_PCT}% line {TRAIL_PCT}% | "
        f"HardStop {HARD_STOP_PCT}% | PreStop {PRESTOP_PCT}% | Dust<₩{int(DUST_LIMIT_KRW)} | TOPN={BACKOFF['topn']}"
    )

def start_threads():
    threading.Thread(target=scanner_loop, daemon=True).start()
    threading.Thread(target=manager_loop, daemon=True).start()
    threading.Thread(target=reporter_loop, daemon=True).start()

# import-time autostart (gunicorn)
if not getattr(app, "_bot_started", False):
    init_bot(); start_threads(); app._bot_started = True

if __name__ == "__main__":
    if not getattr(app, "_bot_started", False):
        init_bot(); start_threads(); app._bot_started = True
    port = int(os.environ.get("PORT","10000"))
    app.run(host="0.0.0.0", port=port)

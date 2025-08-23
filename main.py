# main.py — Upbit Multi-Asset Gainer Scanner Bot (24h) — IPR+SafeOrders
import os, time, json, csv, math, requests, pyupbit, threading, traceback, uuid, jwt, random
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request

# ================= Flask =================
app = Flask(__name__)
@app.get("/")        ;  def index():     return "✅ Upbit Gainer Scanner Bot running"
@app.get("/health")  ;  def health():    return jsonify({"ok": True, "ts": datetime.now().isoformat()}), 200
@app.get("/liveness");  def liveness():  return jsonify({"ok": True}), 200

# ---------------- ENV ----------------
ACCESS_KEY, SECRET_KEY = os.getenv("ACCESS_KEY"), os.getenv("SECRET_KEY")
TELEGRAM_TOKEN, TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")

# Capital & risk
CASH_BUFFER_PCT  = float(os.getenv("CASH_BUFFER_PCT", "0.10"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
MIN_ORDER_KRW    = float(os.getenv("MIN_ORDER_KRW", "5500"))
FEE_RATE         = float(os.getenv("FEE_RATE", "0.0005"))

# Scanner
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "45"))
TOPN_INITIAL      = int(os.getenv("TOPN_INITIAL", "40"))
VOL_SPIKE_MULT    = float(os.getenv("VOL_SPIKE_MULT", "1.8"))
LOOKBACK_MIN      = int(os.getenv("LOOKBACK_MIN", "10"))
MIN_PCT_UP        = float(os.getenv("MIN_PCT_UP", "3.5"))
MIN_PRICE_KRW     = float(os.getenv("MIN_PRICE_KRW", "100"))
EXCLUDED_TICKERS  = set([t.strip() for t in os.getenv("EXCLUDED_TICKERS","KRW-BTC,KRW-ETH").split(",") if t.strip()])

# IPR(진입 튜닝)
PB_MIN_PCT        = float(os.getenv("PB_MIN_PCT", "1.5"))   # 고점 대비 최소 되돌림 %
PB_MAX_PCT        = float(os.getenv("PB_MAX_PCT", "4.0"))   # 고점 대비 최대 되돌림 %
EMA_LEN           = int(os.getenv("EMA_LEN", "10"))
RSI_MAX_BUY       = float(os.getenv("RSI_MAX_BUY", "70"))

# TP/SL/Trail
SL_PCT             = float(os.getenv("SL_PCT", "1.2"))
TP_PCT             = float(os.getenv("TP_PCT", "2.5"))
TRAIL_ACTIVATE_PCT = float(os.getenv("TRAIL_ACTIVATE_PCT", "1.0"))
TRAIL_PCT          = float(os.getenv("TRAIL_PCT", "1.5"))
PARTIAL_TP_RATIO   = float(os.getenv("PARTIAL_TP_RATIO", "0.5"))

# Reporter
REPORT_TZ   = os.getenv("REPORT_TZ", "Asia/Seoul")
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))
REPORT_MINUTE= int(os.getenv("REPORT_MINUTE", "0"))
REPORT_SENT_FILE = os.getenv("REPORT_SENT_FILE", "./last_report_date.txt")
DUST_KRW    = float(os.getenv("DUST_KRW", "5000"))

# Protection
MANAGER_TICK_MS = int(os.getenv("MANAGER_TICK_MS", "400"))
HARD_STOP_PCT   = float(os.getenv("HARD_STOP_PCT", "2.5"))
NO_TRADE_MIN_AROUND_9 = int(os.getenv("NO_TRADE_MIN_AROUND_9", "3"))

# Persist / Rate-limit
PERSIST_DIR = os.getenv("PERSIST_DIR", "./"); os.makedirs(PERSIST_DIR, exist_ok=True)
CSV_FILE, POS_FILE = os.path.join(PERSIST_DIR, "trades.csv"), os.path.join(PERSIST_DIR, "pos.json")
REQ_CHUNK, SLEEP_TICKER, SLEEP_OHLCV = 90, 0.10, 0.10

# ================= Globals =================
KST = timezone(timedelta(hours=9))
UPBIT = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY) if (ACCESS_KEY and SECRET_KEY) else None
POS_LOCK = threading.Lock()
POS, COOLDOWN = {}, {}
BACKOFF = {"topn": TOPN_INITIAL, "scan_interval": SCAN_INTERVAL_SEC, "sleep_ticker": SLEEP_TICKER, "sleep_ohlcv": SLEEP_OHLCV}

# ================= Telegram =================
def _post_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=5)
    except Exception as e: print(f"[TG_FAIL] {e} | {text}")
def send_telegram(msg: str): _post_telegram(msg)

# --------- TG format helpers (동일) ---------
def tg_buy(t,q,avg,used): send_telegram(f"🚀 매수 체결\n— 심볼: {t}\n— 수량: {q:.6f}\n— 체결가: ₩{avg:,.4f}\n— 투자금액: ₩{used:,.0f}")
def tg_partial(t,qty,avg_sell,real,pct,left): send_telegram(f"✅ 부분 익절 매도 (50%)\n— 심볼: {t}\n— 수량: {qty:.6f}\n— 매도가: ₩{avg_sell:,.4f}\n— 실현수익: ₩{real:,.0f} ({pct:.2f}%)\n— 잔여 보유: {left:.6f}")
def tg_trailing(t,avg_sell,filled,pct): send_telegram(f"📉 트레일링 매도\n— 심볼: {t}\n— 수량: {filled:.6f}\n— 매도가: ₩{avg_sell:,.4f}\n— 실현손익률: {pct:.2f}%\n— 상태: 포지션 종료")
def tg_stop(t,avg_sell,filled,pct): send_telegram(f"⚠️ 손절 매도\n— 심볼: {t}\n— 수량: {filled:.6f}\n— 매도가: ₩{avg_sell:,.4f}\n— 손익률: {pct:.2f}%\n— 상태: 포지션 종료")
def tg_cooldown(t,reason,minutes): send_telegram(f"⏳ 쿨다운 적용\n— 심볼: {t}\n— 종료 사유: {reason}\n— 쿨다운: {minutes}분")

# ================= Utils / IO =================
def now_kst(): return datetime.now(tz=KST)
def now_str(): return now_kst().strftime("%Y-%m-%d %H:%M:%S")

def get_price_safe(t, tries=3, delay=0.6):
    for i in range(tries):
        try:
            p = pyupbit.get_current_price(t)
            if p is not None: return float(p)
        except Exception as e: print(f"[price:{t}] {e}")
        time.sleep(delay*(i+1))
    return None

def get_ohlcv_safe(t, count=30, interval="minute1", tries=3, delay=0.6):
    for i in range(tries):
        try:
            df = pyupbit.get_ohlcv(t, interval=interval, count=count)
            if df is not None and not df.empty: return df
        except Exception as e: print(f"[ohlcv:{t}] {e}")
        time.sleep(delay*(i+1))
    return None

def save_pos():
    with POS_LOCK:
        obj = {t: {"qty": float(p.get("qty",0.0)), "avg": float(p.get("avg",0.0)),
                   "entry_ts": p.get("entry_ts"), "highest": float(p.get("highest",0.0)),
                   "trail_active": bool(p.get("trail_active", False)),
                   "partial_tp_done": bool(p.get("partial_tp_done", False)),
                   "cooldown_until": float(p.get("cooldown_until", 0.0))} for t,p in POS.items()}
    tmp = POS_FILE + ".tmp"; open(tmp, "w", encoding="utf-8").write(json.dumps(obj, ensure_ascii=False, indent=2)); os.replace(tmp, POS_FILE)
def load_pos():
    if not os.path.exists(POS_FILE): return
    try: obj = json.load(open(POS_FILE,"r",encoding="utf-8"))
    except Exception: return
    with POS_LOCK:
        POS.clear()
        for t,p in obj.items():
            POS[t] = {"qty": float(p.get("qty",0.0)), "avg": float(p.get("avg",0.0)),
                      "entry_ts": p.get("entry_ts"), "highest": float(p.get("highest",0.0)),
                      "trail_active": bool(p.get("trail_active", False)),
                      "partial_tp_done": bool(p.get("partial_tp_done", False)),
                      "cooldown_until": float(p.get("cooldown_until", 0.0))}

def append_csv(row):
    header = ["ts","ticker","side","qty","price","krw","fee","pnl_krw","pnl_pct","note"]
    exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header); 
        if not exists: w.writeheader()
        w.writerow(row)

# =============== Exchange helpers ===============
def get_balance_krw():
    try: return float(UPBIT.get_balance("KRW") or 0.0)
    except Exception: return 0.0
def get_balance_coin(ticker):
    try: return float(UPBIT.get_balance(ticker) or 0.0)
    except Exception: return 0.0

# =============== Safe Order Module (BUY/SELL + Dust) ===============
from collections import defaultdict
_last_order_at, _symbol_cool, _lock_per_sym = defaultdict(lambda:0.0), defaultdict(lambda:0.0), defaultdict(lambda: threading.Lock())
ORDER_COOLDOWN, MAX_RETRIES, BASE_SLEEP, DUST_LIMIT = 3.0, 5, 0.6, 2000.0

def _now(): return time.time()
def _round_down_qty(q, d=6): 
    if q<=0: return 0.0
    f=10**d; return math.floor(q*f)/f
def _get_price_with_retry(m, n=4):
    wait=0.2
    for _ in range(n):
        px = get_price_safe(m)
        if isinstance(px,(int,float)) and px>0: return px
        time.sleep(wait); wait*=1.6
    return None
def _enforce_rate_limit(sym):
    since = _now()-_last_order_at[sym]
    if since<ORDER_COOLDOWN: time.sleep(ORDER_COOLDOWN-since)

def safe_sell_market(upbit, market:str, portion:float=1.0):
    sym = market.split("-")[1].upper(); lock = _lock_per_sym[sym]
    with lock:
        try:
            bal = get_balance_coin(sym)
            if bal<=0: return {"status":"EMPTY","resp":None}
            price = _get_price_with_retry(market); 
            if not price: return {"status":"FAIL","reason":"price_fetch_error","resp":None}
            qty = _round_down_qty(bal*portion,6); est = qty*price
            # dust 전량 정리
            if est < MIN_ORDER_KRW:
                if est < DUST_LIMIT:
                    resp=None
                    for _ in range(3):
                        try: resp = upbit.sell_market_order(market, bal)
                        except Exception: resp=None
                        if resp: break
                        time.sleep(0.5)
                    return {"status":"DUST_CLEAN" if resp else "FAIL","resp":resp}
                return {"status":"SKIP","reason":"under_min_order","resp":None}
            # 정상 매도
            resp=None; wait=BASE_SLEEP
            for _ in range(MAX_RETRIES):
                _enforce_rate_limit(sym)
                try: resp = upbit.sell_market_order(market, qty)
                except Exception: resp=None
                _last_order_at[sym]=_now()
                if resp: break
                time.sleep(wait); wait*=1.5
            return {"status":"OK","resp":resp} if resp else {"status":"FAIL","reason":"resp_none_all_retry","resp":None}
        except Exception as e:
            traceback.print_exc(); return {"status":"ERROR","reason":str(e),"resp":None}

def safe_buy_market(upbit, market:str, krw_amount:float):
    sym = market.split("-")[1].upper(); lock=_lock_per_sym[sym]
    with lock:
        try:
            if krw_amount < MIN_ORDER_KRW: return {"status":"SKIP","reason":"under_min_order","resp":None}
            price = _get_price_with_retry(market)
            if not price: return {"status":"FAIL","reason":"price_fetch_error","resp":None}
            krw_before = get_balance_krw(); coin_before = get_balance_coin(sym)
            resp=None; wait=BASE_SLEEP
            for _ in range(MAX_RETRIES):
                _enforce_rate_limit(sym)
                try: resp = upbit.buy_market_order(market, krw_amount*0.9990)
                except Exception: resp=None
                _last_order_at[sym]=_now()
                if resp: break
                time.sleep(wait); wait*=1.5
            if not resp: return {"status":"FAIL","reason":"resp_none_all_retry","resp":None}
            # 체결 확인
            t0=time.time()
            while time.time()-t0 < 30:
                if get_balance_coin(sym) > coin_before + 1e-9: break
                time.sleep(0.6)
            coin_after = get_balance_coin(sym); qty_filled = coin_after-coin_before
            krw_after = get_balance_krw(); spent = max(0.0, krw_before-krw_after)
            avg = (spent/qty_filled) if qty_filled>0 else price
            return {"status":"OK","resp":resp,"avg":avg,"qty":qty_filled,"spent":spent}
        except Exception as e:
            traceback.print_exc(); return {"status":"ERROR","reason":str(e),"resp":None}

# =============== Indicators (EMA/RSI) ===============
def ema(series, length):
    import pandas as pd
    s = pd.Series(series)
    return s.ewm(span=length, adjust=False).mean().tolist()

def rsi14_from_ohlcv(df):
    import pandas as pd
    delta = df['close'].diff()
    up = delta.clip(lower=0); down = -delta.clip(upper=0)
    roll_up = up.ewm(span=14, adjust=False).mean()
    roll_down = down.ewm(span=14, adjust=False).mean()
    rs = roll_up / (roll_down+1e-12)
    rsi = 100 - (100/(1+rs))
    return rsi.tolist()

# =============== Backoff =================
def on_rate_error():
    BACKOFF["topn"] = max(15, BACKOFF["topn"]-5)
    BACKOFF["scan_interval"] = min(90, BACKOFF["scan_interval"]+15)
    BACKOFF["sleep_ticker"] *= 1.5; BACKOFF["sleep_ohlcv"] *= 1.5
    send_telegram(f"🧯 백오프 적용: TOPN={BACKOFF['topn']}, scan={BACKOFF['scan_interval']}s")

# =============== Scanner (IPR 진입) ===============
TICKER_URL = "https://api.upbit.com/v1/ticker"
_last_scan_summary_ts = 0.0

def fetch_top_by_turnover(krw_tickers, topn):
    res=[]
    try:
        for i in range(0,len(krw_tickers),REQ_CHUNK):
            chunk=krw_tickers[i:i+REQ_CHUNK]
            r=requests.get(TICKER_URL, params={"markets": ",".join(chunk)}, timeout=5)
            if r.status_code==429: on_rate_error(); continue
            r.raise_for_status()
            data=r.json()
            for d in data:
                res.append({"market": d["market"], "price": float(d["trade_price"]),
                            "turnover24h": float(d.get("acc_trade_price_24h",0.0))})
            time.sleep(BACKOFF["sleep_ticker"])
        res.sort(key=lambda x:x["turnover24h"], reverse=True)
        return res[:topn]
    except Exception as e: send_telegram(f"⚠️ 거래대금 조회 실패: {e}"); return []

def scan_once_and_maybe_buy():
    global _last_scan_summary_ts
    # 09:00 보호
    kst=now_kst()
    if kst.hour==9 and kst.minute < NO_TRADE_MIN_AROUND_9:
        if time.time()-_last_scan_summary_ts>600:
            _last_scan_summary_ts=time.time(); send_telegram("⏸ 09:00 변동성 보호로 신규 진입 스킵")
        return

    # 슬롯 확인
    with POS_LOCK:
        open_cnt=sum(1 for _,p in POS.items() if p.get("qty",0.0)>0.0)
    free_slots=max(0, MAX_OPEN_POSITIONS-open_cnt)
    if free_slots<=0:
        if time.time()-_last_scan_summary_ts>600:
            _last_scan_summary_ts=time.time(); send_telegram("🔎 스캔요약: 후보 0 / free_slots=0 (슬롯 가득)")
        return

    # 유니버스 필터
    try: krw_tickers=[t for t in pyupbit.get_tickers("KRW") if t not in EXCLUDED_TICKERS]
    except Exception as e: send_telegram(f"⚠️ 티커 조회 실패: {e}"); return
    now=time.time(); universe=[]
    for t in krw_tickers:
        if COOLDOWN.get(t,0.0) > now: continue
        with POS_LOCK:
            if t in POS and POS[t].get("qty",0.0)>0.0: continue
        universe.append(t)

    # TOPN
    topN=fetch_top_by_turnover(universe, BACKOFF["topn"])
    if not topN:
        if time.time()-_last_scan_summary_ts>600:
            _last_scan_summary_ts=time.time(); send_telegram(f"🔎 스캔요약: 후보 0 / TOPN={BACKOFF['topn']}")
        return

    # 후보 스코어링 + IPR 진입 조건 사전계산
    cands=[]
    for it in topN:
        t=it["market"]; price=it["price"]
        if price < MIN_PRICE_KRW: continue
        df=get_ohlcv_safe(t, count=max(LOOKBACK_MIN+20, 40))
        if df is None or len(df)<LOOKBACK_MIN+5: continue

        closes=df["close"].tolist(); highs=df["high"].tolist()
        vols=df["volume"].tolist()
        ref=closes[-(LOOKBACK_MIN+1)]; last=closes[-1]
        pct_up=(last-ref)/ref*100.0
        vals=[c*v for c,v in zip(closes,vols)]
        past_avg=sum(vals[-(LOOKBACK_MIN+1):-1]) / LOOKBACK_MIN
        recent=vals[-1]; spike=(recent/past_avg) if past_avg>0 else 0.0
        if pct_up < MIN_PCT_UP or spike < VOL_SPIKE_MULT: continue

        # IPR: 되돌림+재상승
        import pandas as pd
        ema10 = ema(closes, EMA_LEN)
        rsi14 = rsi14_from_ohlcv(df)
        swing_high = max(highs[-(LOOKBACK_MIN//2+5):])   # 최근 절반구간 + 버퍼
        pb_low = swing_high*(1 - PB_MAX_PCT/100.0)
        pb_high= swing_high*(1 - PB_MIN_PCT/100.0)

        # 현재가가 풀백 구간에 있고, EMA10 위이며, 직전 캔들 고점을 소폭 돌파
        last_close = closes[-1]; prev_high = highs[-2]
        ema_ok = last_close >= (ema10[-1] or last_close)
        rsi_ok = (rsi14[-1] if isinstance(rsi14[-1], (int,float)) else 50.0) <= RSI_MAX_BUY
        resumption = last_close > prev_high * 1.001   # 0.1% 미세 돌파

        in_pb_zone = (pb_low <= last_close <= pb_high)

        if in_pb_zone and ema_ok and rsi_ok and resumption:
            score = pct_up*0.7 + min(spike,5.0)*6.0 + 3.0  # 재상승 가점
            cands.append((t, score, last, it["turnover24h"]))
        time.sleep(BACKOFF["sleep_ohlcv"]*(0.8+0.4*random.random()))

    # 자금 산정
    krw_total=get_balance_krw()
    target_used=krw_total*(1.0 - CASH_BUFFER_PCT)
    inuse=0.0
    with POS_LOCK:
        for t,p in POS.items():
            if p.get("qty",0.0)>0:
                pr=get_price_safe(t) or p["avg"]; inuse+=p["qty"]*pr
    krw_left=max(0.0, target_used-inuse)

    if time.time()-_last_scan_summary_ts>600:
        _last_scan_summary_ts=time.time()
        est_per_slot = (krw_left/free_slots) if free_slots>0 else 0.0
        send_telegram(f"🔎 스캔요약: 후보 {len(cands)} / TOPN={BACKOFF['topn']} / free_slots={free_slots} / per_slot≈₩{est_per_slot:,.0f}")

    if not cands or krw_left < MIN_ORDER_KRW: return
    per_slot = krw_left / free_slots
    if per_slot < MIN_ORDER_KRW:
        send_telegram(f"⏸ 매수 스킵: per_slot ₩{per_slot:,.0f} < 최소주문 ₩{MIN_ORDER_KRW:,.0f}"); return

    cands.sort(key=lambda x:(x[1], x[3]), reverse=True)
    picks=cands[:free_slots]
    for (t,_,__,___) in picks:
        br = safe_buy_market(UPBIT, t, per_slot)
        if br.get("status")=="OK" and br.get("qty",0)>0:
            avg, qty, spent = br["avg"], br["qty"], br["spent"]
            with POS_LOCK:
                POS[t]={"qty": float(qty), "avg": float(avg), "entry_ts": now_str(),
                        "highest": float(avg), "trail_active": False, "partial_tp_done": False, "cooldown_until": 0.0}
            save_pos(); tg_buy(t, qty, avg, spent)
            append_csv({"ts": now_str(),"ticker": t,"side":"BUY","qty": qty,"price": avg,
                        "krw": -spent,"fee": spent*FEE_RATE,"pnl_krw":0,"pnl_pct":0,"note": "scanner_entry(IPR)"})

# =============== Manager/Reporter/Boot (기존 로직 유지) ===============
def manage_positions_once():
    with POS_LOCK: items=list(POS.items())
    for t,p in items:
        qty=p.get("qty",0.0)
        if qty<=0: continue
        avg=p.get("avg",0.0); price=get_price_safe(t)
        if not price or avg<=0: continue
        pnl_pct_now=(price-avg)/avg*100.0
        # Emergency hard stop
        if pnl_pct_now <= -HARD_STOP_PCT:
            sr = safe_sell_market(UPBIT, t, 1.0)
            if sr.get("status") in ("OK","DUST_CLEAN"):
                avg_sell = get_price_safe(t) or price
                pnl_pct=((avg_sell-avg)/avg)*100.0
                tg_stop(t, avg_sell, qty, pnl_pct)
                append_csv({"ts": now_str(),"ticker": t,"side":"EMERGENCY_STOP","qty": qty,"price": avg_sell,
                            "krw": qty*avg_sell,"fee": qty*avg_sell*FEE_RATE,"pnl_krw": qty*(avg_sell-avg),
                            "pnl_pct": pnl_pct,"note": f"hard_stop{-HARD_STOP_PCT:.1f}%"})
                with POS_LOCK:
                    POS[t]["qty"]=0.0; POS[t]["cooldown_until"]=time.time()+5400; COOLDOWN[t]=POS[t]["cooldown_until"]
                save_pos()
            continue

        highest=max(p.get("highest",avg), price)
        trail_active=p.get("trail_active", False)
        partial_done=p.get("partial_tp_done", False)
        if (not trail_active) and pnl_pct_now >= TRAIL_ACTIVATE_PCT:
            trail_active=True; send_telegram(f"🛡️ 트레일 활성화: {t} peak={highest:.4f} | line {TRAIL_PCT:.2f}%")

        sl_price=avg*(1 - SL_PCT/100.0)
        trail_line=highest*(1 - TRAIL_PCT/100.0) if trail_active else sl_price
        dyn_sl= max(sl_price, trail_line) if trail_active else sl_price

        if price <= dyn_sl and pnl_pct_now < TP_PCT:
            sr = safe_sell_market(UPBIT, t, 1.0)
            if sr.get("status") in ("OK","DUST_CLEAN"):
                avg_sell=get_price_safe(t) or price
                pnl_pct=((avg_sell-avg)/avg)*100.0
                note="손절" if pnl_pct<0 else "트레일링"
                cd_min= 90 if pnl_pct<0 and not partial_done else 30
                if pnl_pct<0: tg_stop(t, avg_sell, qty, pnl_pct)
                else:         tg_trailing(t, avg_sell, qty, pnl_pct)
                append_csv({"ts": now_str(),"ticker": t,"side":"STOP/TRAIL","qty": qty,"price": avg_sell,
                            "krw": qty*avg_sell,"fee": qty*avg_sell*FEE_RATE,"pnl_krw": qty*(avg_sell-avg),
                            "pnl_pct": pnl_pct,"note":"dyn_sl"})
                with POS_LOCK:
                    POS[t]["qty"]=0.0; POS[t]["cooldown_until"]=time.time()+cd_min*60; COOLDOWN[t]=POS[t]["cooldown_until"]
                save_pos(); tg_cooldown(t, note, cd_min)
            continue

        if (not partial_done) and pnl_pct_now >= TP_PCT:
            sr = safe_sell_market(UPBIT, t, PARTIAL_TP_RATIO)
            if sr.get("status")=="OK":
                avg_sell=get_price_safe(t) or price
                filled = qty*PARTIAL_TP_RATIO
                pnl_pct=((avg_sell-avg)/avg)*100.0
                left_after=max(0.0, qty-filled)
                tg_partial(t, filled, avg_sell, filled*(avg_sell-avg), pnl_pct, left_after)
                append_csv({"ts": now_str(),"ticker": t,"side":"PARTIAL_TP","qty": filled,"price": avg_sell,
                            "krw": filled*avg_sell,"fee": filled*avg_sell*FEE_RATE,"pnl_krw": filled*(avg_sell-avg),
                            "pnl_pct": pnl_pct,"note":"partial@2.5%"})
                with POS_LOCK:
                    POS[t]["qty"]=left_after; POS[t]["partial_tp_done"]=True; POS[t]["highest"]=max(highest, price); POS[t]["trail_active"]=True
                save_pos(); continue

        if trail_active and price <= highest*(1 - TRAIL_PCT/100.0):
            sr = safe_sell_market(UPBIT, t, 1.0)
            if sr.get("status") in ("OK","DUST_CLEAN"):
                avg_sell=get_price_safe(t) or price
                pnl_pct=((avg_sell-avg)/avg)*100.0
                tg_trailing(t, avg_sell, qty, pnl_pct)
                append_csv({"ts": now_str(),"ticker": t,"side":"TRAIL","qty": qty,"price": avg_sell,
                            "krw": qty*avg_sell,"fee": qty*avg_sell*FEE_RATE,"pnl_krw": qty*(avg_sell-avg),
                            "pnl_pct": pnl_pct,"note":"trail_hit"})
                with POS_LOCK:
                    POS[t]["qty"]=0.0; POS[t]["cooldown_until"]=time.time()+(1800 if partial_done else 5400); COOLDOWN[t]=POS[t]["cooldown_until"]
                save_pos(); tg_cooldown(t, "트레일링", 30 if partial_done else 90)
                continue

        with POS_LOCK:
            POS[t]["highest"]=highest; POS[t]["trail_active"]=trail_active

# -------- Reporter (간결화) --------
def tz_now():
    try:
        import zoneinfo
        return datetime.now(zoneinfo.ZoneInfo(REPORT_TZ))
    except Exception:
        return datetime.now()
def read_csv_rows():
    if not os.path.exists(CSV_FILE): return []
    with open(CSV_FILE, newline="", encoding="utf-8") as f: return list(csv.DictReader(f))
def summarize_between(s,e):
    rows=read_csv_rows(); s=s.astimezone(KST); e=e.astimezone(KST)
    realized=0.0; cnt=wins=losses=0; pnl_pct_sum=0.0
    for r in rows:
        ts=r.get("ts",""); 
        try: dt=datetime.fromisoformat(ts.replace(" ","T")).replace(tzinfo=KST)
        except Exception: continue
        if not (s<=dt<=e): continue
        side=(r.get("side") or "")
        if side: cnt+=1; 
        if "PARTIAL" in side or "TRAIL" in side: wins +=1
        if "STOP" in side: losses +=1
        realized += float(str(r.get("pnl_krw","0")).replace(",",""))
        pnl_pct_sum += float(str(r.get("pnl_pct","0")).replace(",",""))
    return {"count":cnt,"wins":wins,"losses":losses,"realized_krw":realized,"avg_pnl_pct": (pnl_pct_sum/cnt if cnt else 0.0),
            "winrate": (wins/cnt*100.0 if cnt else 0.0)}
def build_daily_report():
    now=tz_now(); t9=now.replace(hour=9,minute=0,second=0,microsecond=0)
    if now>=t9: start=t9-timedelta(days=1); end=t9-timedelta(microseconds=1)
    else: start=t9-timedelta(days=2); end=t9-timedelta(days=1, microseconds=1)
    met=summarize_between(start,end)
    krw=get_balance_krw()
    with POS_LOCK: holds=[(t,p) for t,p in POS.items() if p.get("qty",0.0)>0]
    total_hold_val=0.0; lines=[]
    for t,p in holds:
        price=get_price_safe(t) or 0.0
        if price<=0: continue
        val=p["qty"]*price; total_hold_val+=val
        lines.append(f"• {t} qty {p['qty']:.6f} @avg {p['avg']:.4f} / now {price:.4f}")
    msg=(f"📊 [일일 리포트] {now.strftime('%Y-%m-%d %H:%M')} ({REPORT_TZ})\n"
         f"거래수 {met['count']} (승 {met['wins']}/패 {met['losses']} | 승률 {met['winrate']:.1f}%) | 평균손익률 {met['avg_pnl_pct']:.2f}%\n\n"
         f"보유자산(코인) ₩{total_hold_val:,.0f} | 현금 ₩{krw:,.0f} | 총자산 ₩{(krw+total_hold_val):,.0f}")
    return msg
def get_last_report_date():
    try: return open(REPORT_SENT_FILE,"r",encoding="utf-8").read().strip()
    except Exception: return None
def set_last_report_date(s): 
    try: open(REPORT_SENT_FILE,"w",encoding="utf-8").write(s)
    except Exception: pass
def reporter_loop():
    send_telegram("⏰ 리포터 시작")
    while True:
        try:
            now=tz_now(); sched=now.replace(hour=REPORT_HOUR,minute=REPORT_MINUTE,second=15,microsecond=0)
            last=get_last_report_date(); today=now.date().isoformat()
            if now>=sched and last!=today:
                send_telegram(build_daily_report()); set_last_report_date(today); time.sleep(90); continue
            time.sleep(30)
        except Exception: print(f"[reporter] {traceback.format_exc()}"); time.sleep(5)

# =============== Loops/Boot ===============
def scanner_loop():
    send_telegram(f"🔎 스캐너 시작 (24h, TOPN={BACKOFF['topn']})")
    while True:
        try: scan_once_and_maybe_buy()
        except Exception: print(f"[scanner] {traceback.format_exc()}"); on_rate_error()
        time.sleep(BACKOFF["scan_interval"])
def manager_loop():
    send_telegram("🧭 매니저 시작 (SL/Partial/Trailing)")
    while True:
        try: manage_positions_once()
        except Exception: print(f"[manager] {traceback.format_exc()}")
        time.sleep(max(0.1, MANAGER_TICK_MS/1000.0))

def init_bot():
    if not ACCESS_KEY or not SECRET_KEY: raise RuntimeError("ACCESS_KEY/SECRET_KEY not set")
    try:
        payload={'access_key':ACCESS_KEY, 'nonce': str(uuid.uuid4())}
        token=jwt.encode(payload, SECRET_KEY, algorithm='HS256')
        headers={"Authorization": f"Bearer {token}"}
        r=requests.get("https://api.upbit.com/v1/accounts", headers=headers, timeout=10)
        if r.status_code!=200: send_telegram(f"❗️업비트 인증/허용IP/레이트리밋 점검: {r.status_code}")
    except Exception as e: print(f"[diag] {e}")
    load_pos(); send_telegram("🤖 봇 시작됨")
    send_telegram(f"⚙️ thresholds | 10m≥+{MIN_PCT_UP:.1f}% & 1m×≥{VOL_SPIKE_MULT:.2f} | "
                  f"SL={SL_PCT:.2f}% | TP(partial@{TP_PCT:.1f}%, {int(PARTIAL_TP_RATIO*100)}%) | "
                  f"TRAIL act {TRAIL_ACTIVATE_PCT:.1f}% / line {TRAIL_PCT:.1f}% | "
                  f"hard_stop {HARD_STOP_PCT:.1f}% | IPR pb {PB_MIN_PCT:.1f}~{PB_MAX_PCT:.1f}% | RSI≤{RSI_MAX_BUY:.0f} | EMA{EMA_LEN} | "
                  f"manager {MANAGER_TICK_MS}ms | TOPN={BACKOFF['topn']}")

def start_threads():
    threading.Thread(target=scanner_loop, daemon=True).start()
    threading.Thread(target=manager_loop, daemon=True).start()
    threading.Thread(target=reporter_loop, daemon=True).start()

if not getattr(app, "_bot_started", False):
    init_bot(); start_threads(); app._bot_started=True

if __name__=="__main__":
    if not getattr(app,"_bot_started", False):
        init_bot(); start_threads(); app._bot_started=True
    port=int(os.environ.get("PORT","10000")); app.run(host="0.0.0.0", port=port)

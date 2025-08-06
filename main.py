import pyupbit
import time
import threading
import asyncio
from datetime import datetime
from telegram import Bot
from flask import Flask
import os
import csv

# 🔐 직접 입력
ACCESS_KEY = "lOmAytTKb4QJpEsWpDWyOcBHtyAfEod2vxjgesBF"
SECRET_KEY = "VtAJf1FZfiH2kmV1AdKFoaaePaH1xqeTFzxDw45O"
TELEGRAM_TOKEN = "8358935066:AAEkuHKK-pP6lgaiFwafH-kceW_1Sfc-EOc"
TELEGRAM_CHAT_ID = "1054008930"

symbol = "KRW-XRP"
profit_ratio = 0.03
loss_ratio = 0.01

csv_file = "trades.csv"
success_count = 0
fail_count = 0
total_profit_percent = 0
last_report_date = None
bought = False
buy_price = 0

# ✅ 텔레그램 알림
async def send_telegram_message_async(msg):
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
    except Exception as e:
        print(f"[텔레그램 전송 실패] {e}")

def send_telegram_message(msg):
    asyncio.run(send_telegram_message_async(msg))

# ✅ 거래 기록 저장
def save_trade(buy_price, sell_price, amount, result, timestamp):
    global total_profit_percent, success_count, fail_count
    profit_percent = ((sell_price - buy_price) / buy_price) * 100
    total_profit_percent += profit_percent

    if result == "익절":
        success_count += 1
    elif result == "손절":
        fail_count += 1

    file_exists = os.path.isfile(csv_file)
    with open(csv_file, mode='a', newline='') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["시간", "매수가", "매도가", "보유량", "결과", "수익률"])
        writer.writerow([timestamp, buy_price, sell_price, amount, result, f"{profit_percent:.2f}%"])

# ✅ 전략 요약
def send_summary():
    total = success_count + fail_count
    if total == 0:
        rate = 0
    else:
        rate = (success_count / total) * 100
    msg = (
        f"📊 자동매매 전략 요약\n"
        f"✅ 익절 횟수: {success_count}\n"
        f"❌ 손절 횟수: {fail_count}\n"
        f"📈 누적 수익률: {total_profit_percent:.2f}%\n"
        f"🎯 전략 성공률: {rate:.2f}%"
    )
    send_telegram_message(msg)

# ✅ 자동매매
def run_bot():
    global last_report_date, bought, buy_price

    upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)
    send_telegram_message("🚀 XRP 자동매매 봇 시작됨")

    while True:
        try:
            now = datetime.now()
            price = pyupbit.get_current_price(symbol)

            # 수익 요약 (오전 9시 1회)
            if last_report_date != now.date() and now.hour == 9:
                send_summary()
                last_report_date = now.date()

            if not bought:
                krw_balance = upbit.get_balance("KRW")
                if krw_balance > 5000:
                    buy_amount = krw_balance * 0.9995
                    upbit.buy_market_order(symbol, buy_amount)
                    buy_price = price
                    bought = True

                    xrp_balance = upbit.get_balance("XRP")
                    send_telegram_message(f"📥 매수 진입! 가격: {buy_price:.2f}\nXRP: {xrp_balance:.4f}")
            else:
                balance = upbit.get_balance("XRP")
                target_profit = buy_price * (1 + profit_ratio)
                target_loss = buy_price * (1 - loss_ratio)

                if price >= target_profit:
                    upbit.sell_market_order(symbol, balance)
                    send_telegram_message(f"🎯 익절 성공! 매도가: {price:.2f}")
                    save_trade(buy_price, price, balance, "익절", now.strftime('%Y-%m-%d %H:%M:%S'))
                    bought = False

                elif price <= target_loss:
                    upbit.sell_market_order(symbol, balance)
                    send_telegram_message(f"💥 손절 처리! 매도가: {price:.2f}")
                    save_trade(buy_price, price, balance, "손절", now.strftime('%Y-%m-%d %H:%M:%S'))
                    bought = False

        except Exception as e:
            send_telegram_message(f"⚠️ 오류 발생: {e}")

        time.sleep(10)

# ✅ Flask 웹서버 → Render 자동 슬립 방지
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ XRP 자동매매 봇이 작동 중입니다."

# ✅ 실행
if __name__ == "__main__":
    # 봇은 백그라운드에서 실행
    threading.Thread(target=run_bot, daemon=True).start()
    # Flask는 Render에서 keep-alive 유지용
    app.run(host="0.0.0.0", port=10000)


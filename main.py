import os
import time
import requests
import pyupbit
import threading
from datetime import datetime
from flask import Flask

app = Flask(__name__)

@app.route("/")
def index():
    return "✅ Yul Bot is running on Render (Web Service)"

ACCESS_KEY = os.getenv("ACCESS_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

symbol = "KRW-XRP"
profit_ratio = 0.03
loss_ratio = 0.01
bought = False
buy_price = None
last_report_date = None

def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        requests.post(url, data=payload)
    except Exception as e:
        print("🚨 텔레그램 전송 오류:", e)

def daily_report(success_count, fail_count, total_profit_percent):
    total = success_count + fail_count
    rate = (success_count / total) * 100 if total > 0 else 0
    msg = (
        f"📊 자동매매 리포트\n"
        f"✅ 익절 횟수: {success_count}\n"
        f"❌ 손절 횟수: {fail_count}\n"
        f"📈 누적 수익률: {total_profit_percent:.2f}%\n"
        f"🎯 전략 성공률: {rate:.2f}%"
    )
    send_telegram_message(msg)

def run_bot():
    global bought, buy_price, last_report_date
    success_count = 0
    fail_count = 0
    total_profit_percent = 0

    upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)
    send_telegram_message("🤖 자동매매 봇 실행됨 (Render Web Service)")

    while True:
        try:
            now = datetime.now()
            price = pyupbit.get_current_price(symbol)

            if price is None:
                time.sleep(10)
                continue

            if last_report_date != now.date() and now.hour == 9:
                daily_report(success_count, fail_count, total_profit_percent)
                last_report_date = now.date()

            if not bought:
                try:
                    krw = upbit.get_balance("KRW")
                    if krw is not None and krw > 5000:
                        upbit.buy_market_order(symbol, krw * 0.9995)
                        buy_price = price
                        bought = True
                        send_telegram_message(f"📥 매수 진입: {buy_price:.2f}원")
                    else:
                        send_telegram_message(f"❗️KRW 잔액 부족: {krw}")
                        time.sleep(60)
                        continue
                except Exception as buy_err:
                    send_telegram_message(f"❗️매수 주문 실패: {buy_err}")
                    time.sleep(60)
                    continue
            else:
                try:
                    upbit.sell_market_order(symbol, 0)
                except Exception as sell_err:
                    send_telegram_message(f"❗️매도 주문 실패: {sell_err}")
                    time.sleep(60)
                    continue

                if buy_price is None:
                    send_telegram_message("❗ buy_price가 None입니다. 거래 스킵.")
                    time.sleep(10)
                    continue

                profit_percent = ((price - buy_price) / buy_price) * 100
                target_profit = buy_price * (1 + profit_ratio)
                target_loss = buy_price * (1 - loss_ratio)

                if price >= target_profit:
                    success_count += 1
                    total_profit_percent += profit_percent
                    send_telegram_message(f"🎯 익절 완료: {price:.2f}원 (+{profit_percent:.2f}%)")
                    bought = False
                    buy_price = None

                elif price <= target_loss:
                    fail_count += 1
                    total_profit_percent += profit_percent
                    send_telegram_message(f"💥 손절 처리: {price:.2f}원 ({profit_percent:.2f}%)")
                    bought = False
                    buy_price = None

        except Exception as e:
            send_telegram_message(f"⚠️ 오류 발생: {e}")
            time.sleep(10)

        time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=run_bot).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

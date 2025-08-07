import os
import time
import requests
import pyupbit
from datetime import datetime

# ✅ 환경변수에서 텔레그램 및 업비트 정보 불러오기
TELEGRAM_TOKEN = os.getenv("8358935066:AAEkuHKK-pP6lgaiFwafH-kceW_1Sfc-EOc")
TELEGRAM_CHAT_ID = os.getenv("1054008930")
ACCESS_KEY = os.getenv("lOmAytTKb4QJpEsWpDWyOcBHtyAfEod2vxjgesBF")
SECRET_KEY = os.getenv("VtAJf1FZfiH2kmV1AdKFoaaePaH1xqeTFzxDw45O")


# ✅ 기본 설정
symbol = "KRW-XRP"
profit_ratio = 0.03  # 3% 익절
loss_ratio = 0.01    # 1% 손절
bought = False
buy_price = 0
last_report_date = None

# ✅ 텔레그램 메시지 전송 함수
def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        requests.post(url, data=payload)
    except Exception as e:
        print("🚨 텔레그램 전송 오류:", e)

# ✅ 리포트 전송 함수 (매일 오전 9시)
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

# ✅ 메인 자동매매 루프
def run_bot():
    global bought, buy_price, last_report_date
    success_count = 0
    fail_count = 0
    total_profit_percent = 0

    upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)
    send_telegram_message("🤖 자동매매 봇 실행됨")

    while True:
        try:
            now = datetime.now()
            price = pyupbit.get_current_price(symbol)

            # 📌 오전 9시 리포트
            if last_report_date != now.date() and now.hour == 9:
                daily_report(success_count, fail_count, total_profit_percent)
                last_report_date = now.date()

            if not bought:
                krw = upbit.get_balance("KRW")
                if krw > 5000:
                    upbit.buy_market_order(symbol, krw * 0.9995)
                    buy_price = price
                    bought = True
                    send_telegram_message(f"📥 매수 진입: {buy_price:.2f}원")
            else:
                xrp_balance = upbit.get_balance("XRP")
                target_profit = buy_price * (1 + profit_ratio)
                target_loss = buy_price * (1 - loss_ratio)

                if price >= target_profit:
                    upbit.sell_market_order(symbol, xrp_balance)
                    profit = ((price - buy_price) / buy_price) * 100
                    success_count += 1
                    total_profit_percent += profit
                    send_telegram_message(f"🎯 익절 완료: {price:.2f}원 (+{profit:.2f}%)")
                    bought = False

                elif price <= target_loss:
                    upbit.sell_market_order(symbol, xrp_balance)
                    loss = ((price - buy_price) / buy_price) * 100
                    fail_count += 1
                    total_profit_percent += loss
                    send_telegram_message(f"💥 손절 처리: {price:.2f}원 ({loss:.2f}%)")
                    bought = False

        except Exception as e:
            send_telegram_message(f"⚠️ 오류 발생: {e}")

        time.sleep(10)

if __name__ == "__main__":
    run_bot()


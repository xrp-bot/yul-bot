import time
import requests

# 텔레그램 설정
TELEGRAM_TOKEN = "8358935066:AAEkuHKK-pP6lgaiFwafH-kceW_1Sfc-EOc"
TELEGRAM_CHAT_ID = "1054008930"


def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {'chat_id': TELEGRAM_CHAT_ID, 'text': message}
    requests.post(url, data=data)

def run_bot():
    send_telegram_message("🤖 자동매매 봇이 시작되었습니다.")
    while True:
        # 여기에 나만의 매매 전략을 추가하세요
        print("자동매매 실행 중...")
        time.sleep(60)

if __name__ == "__main__":
    run_bot()

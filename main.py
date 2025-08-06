import time
import threading
import requests
from flask import Flask

# 🔐 텔레그램 설정
TELEGRAM_TOKEN = "8358935066:AAEkuHKK-pP6lgaiFwafH-kceW_1Sfc-EOc"
TELEGRAM_CHAT_ID = "1054008930"

# 텔레그램 메시지 전송 함수
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {'chat_id': TELEGRAM_CHAT_ID, 'text': message}
    requests.post(url, data=data)

# 자동매매 봇 로직
def run_bot():
    send_telegram_message("🤖 자동매매 봇이 시작되었습니다.")
    while True:
        print("자동매매 실행 중...")
        time.sleep(60)

# Flask 서버로 UptimeRobot 응답
app = Flask(__name__)

@app.route('/')
def keep_alive():
    return "✅ Bot is alive!"

# 메인 실행
if __name__ == "__main__":
    threading.Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=10000)

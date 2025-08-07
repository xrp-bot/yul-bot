# Python 3.12을 기반 이미지로 설정
FROM python:3.12-slim

# 작업 디렉토리 설정
WORKDIR /app

# 현재 디렉토리의 모든 파일을 컨테이너로 복사
COPY . .

# pip 최신화 후 필요한 패키지들을 정확한 버전으로 설치 (캐시 미사용)
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir python-telegram-bot==13.15 flask requests pyupbit

# main.py 실행
CMD ["python", "main.py"]

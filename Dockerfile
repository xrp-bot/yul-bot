FROM python:3.12-slim

WORKDIR /app

# 기본 패키지 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && \
    rm -rf /var/lib/apt/lists/*

# requirements 설치 전에 urllib3와 python-telegram-bot을 명확하게 고정
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir python-telegram-bot==13.15 urllib3==1.26.16
RUN pip install --no-cache-dir -r requirements.txt

# 나머지 파일 복사
COPY . .

CMD ["python", "main.py"]

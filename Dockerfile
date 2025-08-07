FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir python-telegram-bot==13.15 flask requests pyupbit

CMD ["python", "main.py"]

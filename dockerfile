FROM python:3.12-slim

# 文字化け/時刻ずれ防止
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Tokyo

WORKDIR /app

# 依存だけ先に入れてキャッシュを効かせる
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリ本体
COPY . .

# 本番は Railway の Variables から環境変数を渡すので .env は不要
# そのまま起動
CMD ["python", "bot.py"]

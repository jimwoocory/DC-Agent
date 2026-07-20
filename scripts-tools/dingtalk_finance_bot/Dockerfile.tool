FROM python:3.11-slim

RUN pip install --no-cache-dir dingtalk-stream httpx openpyxl

WORKDIR /app

EXPOSE 6193

CMD ["python", "finance_bot.py", "serve-tool", "--host", "0.0.0.0", "--port", "6193", "--auto-scan-interval", "0", "--auto-scan-limit", "20", "--stream-events"]

FROM python:3.11-slim

RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    openpyxl \
    requests beautifulsoup4 trafilatura \
    ddgs

WORKDIR /app
ENV PYTHONPATH=/app

CMD ["python", "--version"]

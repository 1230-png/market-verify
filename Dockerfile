FROM python:3.12-slim

ENV TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# ffmpeg: MoviePy 렌더링 / tzdata: Asia/Seoul 시간대 / fonts-nanum: 한글 자막이 tofu(□)로 깨지지 않게
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tzdata \
        fonts-nanum \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

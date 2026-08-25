FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 TZ=Asia/Almaty
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
COPY rules.yaml .
EXPOSE 8000
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]

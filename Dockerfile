FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/app.py .
COPY static/ static/

EXPOSE 8080

RUN pip install gunicorn
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "app:app"]
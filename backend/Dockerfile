FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render/Railway/Fly sætter selv $PORT — fald tilbage til 8000 lokalt
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]

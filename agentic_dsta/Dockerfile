# Use a slim base image to reduce the attack surface
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt /app/
# Use --no-cache-dir to reduce image size
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Create a non-root user to run the application
RUN adduser --disabled-password --gecos "" myuser

COPY . /app/agentic_dsta
# Set ownership of the application files to the non-root user
RUN chown -R myuser:myuser /app && chmod +x /app/agentic_dsta/main.py

ENV PYTHONPATH=/app

# Switch to the non-root user
USER myuser

ENV PATH="/home/myuser/.local/bin:$PATH"
ENV GOOGLE_CLOUD_LOCATION="us-central1"
# Cloud Run runs in us-central1, but no Gemini 3.x model is served from any
# single US region. Model calls go to the `us` multi-region endpoint
# (https://aiplatform.us.rep.googleapis.com). Set to "global" to trade US data
# residency for the widest capacity pool.
ENV GEMINI_LOCATION="us"
ENV GOOGLE_GENAI_USE_VERTEXAI="True"
ENV PYTHONUNBUFFERED=1


CMD ["sh", "-c", "uvicorn agentic_dsta.main:app --host 0.0.0.0 --port $PORT"]
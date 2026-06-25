# Dockerfile for Hugging Face Spaces (Docker SDK Space)
# HF Spaces requires:
#   - Non-root user (uid 1000)
#   - App listening on port 7860

FROM python:3.11-slim

# ── System dependencies ────────────────────────────────────────
# tesseract / poppler needed for OCR; libgomp for sentence-transformers
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    tesseract-ocr \
    tesseract-ocr-eng \
    libtesseract-dev \
    poppler-utils \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user (HF requirement) ────────────────────────────
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /app

# ── Install Python deps ────────────────────────────────────────
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -c "import nltk; nltk.download('wordnet'); nltk.download('omw-1.4')"

# ── Copy application ───────────────────────────────────────────
COPY --chown=user . .

# ── Runtime ───────────────────────────────────────────────────
EXPOSE 7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]

# Secondary path only. `make demo` is the supported entry point and the one
# that is actually verified — see README > Limitations. Docker was unavailable
# on the development machine, so this image is UNTESTED.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY aegis/ ./aegis/
COPY policies/ ./policies/
COPY scenarios/ ./scenarios/
COPY tests/ ./tests/
COPY pytest.ini Makefile ./

ENV AEGIS_BACKEND=mock \
    AEGIS_SEED=1337 \
    AEGIS_DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Fit the lane models at build time so the container starts ready.
RUN python -m aegis.feedback.trainer --fit

CMD ["uvicorn", "aegis.main:app", "--host", "0.0.0.0", "--port", "8000"]

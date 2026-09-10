# One process, one market engine. See docs/architecture.md for why that is not
# negotiable: a second instance means a second clock and a second price for the
# same stock at the same moment.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir -e .

COPY config ./config
COPY web ./web
COPY scripts ./scripts

EXPOSE 8000
ENV EXCHANGE_HOST=0.0.0.0 EXCHANGE_PORT=8000 EXCHANGE_SECURE_COOKIES=true

# Create the schema and load the basket, then run. The seed only creates what
# is missing, so a restart never wipes a competition in progress.
CMD ["sh", "-c", "python -m scripts.seed && python -m app.main"]

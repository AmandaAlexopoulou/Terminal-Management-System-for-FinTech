# ============================================================================
# Dockerfile — tms-cron (Bonus: nightly decommission cleanup)
# ============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /cron

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Δεν χρειάζεται EXPOSE — το container δεν εκθέτει κανένα port, μιλάει
# μόνο "προς τα έξω" στη MySQL, όχι "προς τα μέσα" σε κανέναν client.

# Entrypoint: το ίδιο process (cleanup.py) παραμένει "foreground" για πάντα
# (βλ. το while True loop μέσα στο script) — αυτό κρατάει το container "up".
CMD ["python", "cleanup.py"]

# ============================================================================
# Dockerfile — TMS API (Flask)
# ============================================================================
FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE: μη γράφεις .pyc αρχεία μέσα στο container (δεν
# τα χρειαζόμαστε, μόνο πιάνουν χώρο).
# PYTHONUNBUFFERED: τα print()/logging να φτάνουν ΑΜΕΣΩΣ στο stdout, χωρίς
# buffering — κρίσιμο ώστε το `docker compose logs -f` να δείχνει logs σε
# real time και όχι με καθυστέρηση.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Αντιγράφουμε ΠΡΩΤΑ μόνο το requirements.txt και κάνουμε install. Έτσι το
# Docker layer caching δεν ξανακατεβάζει όλα τα pip packages κάθε φορά που
# αλλάζει μόνο ο κώδικας (main.py) — μόνο όταν αλλάζουν τα dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Τώρα αντιγράφουμε τον υπόλοιπο κώδικα της εφαρμογής.
COPY . .

EXPOSE 5000

# Entrypoint: τρέχει το main.py (το ίδιο το Flask app κάνει app.run()).
CMD ["python", "main.py"]

"""
main.py — Terminal Management System (TMS) API
================================================

Entrypoint του Flask API. Εδώ:
  - Στήνουμε logging (ΠΑΝΤΑ σε stdout, με timestamp + level + message).
  - Δημιουργούμε connections προς MySQL (μέσω SQLAlchemy) και Redis.
  - Κάνουμε "safe" / idempotent migrations: προσθέτουμε τη στήλη
    `updated_on` που δεν υπάρχει στο επίσημο schema, και δημιουργούμε το
    `decommission_queue`.
  - Ορίζουμε τα endpoints:
      Feature A (A1-A5) — terminals
      Feature B (B1-B3) — templates + create-from-template
      Feature C          — Redis cache-aside + invalidation (χρησιμοποιείται
                            ΜΕΣΑ στα A1 και D1-D4, όχι ξεχωριστά endpoints)
      Feature D (D1-D4)  — στατιστικά με Pandas
      + το γενικό /health

Design decision: χρησιμοποιούμε το SQLAlchemy ΜΟΝΟ ως connection-pool
manager + query builder (Core API, με text() + bind parameters), ΟΧΙ ως
πλήρες ORM με model classes. Αυτό μας δίνει connection pooling και
parameterized queries (technical requirement #3) χωρίς την επιπλέον
πολυπλοκότητα ενός πλήρους ORM layer.
"""

import os
import sys
import io
import json
import logging
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, Response
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
import redis
import pandas as pd
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# 1) Environment variables
# ----------------------------------------------------------------------------
# Μέσα σε Docker τα env vars έρχονται ήδη από το docker-compose.yml
# (environment: block). Κάνουμε ΚΑΙ load_dotenv() ώστε το ίδιο main.py να
# μπορεί να τρέξει και LOCAL (εκτός container), διαβάζοντας ένα .env αρχείο
# από τον τρέχοντα φάκελο — π.χ. για γρήγορο manual testing.
load_dotenv()

DB_HOST = os.getenv("DB_HOST", "mysql")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("MYSQL_DATABASE")
DB_USER = os.getenv("MYSQL_USER")
DB_PASSWORD = os.getenv("MYSQL_PASSWORD")

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

APP_PORT = int(os.getenv("APP_PORT", "5000"))

# ----------------------------------------------------------------------------
# 2) Logging — technical requirement #1: ΟΛΑ τα logs στο stdout (ΟΧΙ σε
#    αρχεία), με timestamp, log level, και μήνυμα.
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("tms-api")

# ----------------------------------------------------------------------------
# 3) Flask app
# ----------------------------------------------------------------------------
app = Flask(__name__)

# ----------------------------------------------------------------------------
# 4) Database engine (SQLAlchemy)
# ----------------------------------------------------------------------------
DB_URL = f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# pool_pre_ping=True: πριν δοθεί ένα connection από το pool, το SQLAlchemy
# κάνει ένα γρήγορο "SELECT 1" για να βεβαιωθεί ότι δεν είναι "νεκρό" (π.χ.
# λόγω restart της MySQL ή idle timeout) — αλλιώς θα παίρναμε τυχαία,
# δύσκολα αναπαραγώγιμα connection errors.
# pool_recycle=280: ανακυκλώνει connections πριν φτάσουν στο default
# MySQL "wait_timeout" (συνήθως 28800s = 8h, αλλά καλή πρακτική έτσι κι
# αλλιώς σε containerized περιβάλλοντα).
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=280)

# ----------------------------------------------------------------------------
# 5) Redis client
# ----------------------------------------------------------------------------
redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,  # να παίρνουμε πίσω str αντί για bytes
    socket_connect_timeout=3,
)


# Όλα τα cached keys μοιράζονται το ίδιο prefix. Αυτό μας επιτρέπει να
# κάνουμε "καθαρισμό ολόκληρου του cache" (Feature C requirement #2) με ένα
# scoped SCAN+DELETE πάνω σε "cache:*", αντί για ένα ωμό FLUSHDB που θα
# έσβηνε τα πάντα μέσα στο Redis instance, ό,τι κι αν ήταν αυτό.
CACHE_KEY_PREFIX = "cache:"


def cache_get(key: str):
    """
    Feature C — cache-aside, βήμα "read".

    Επιστρέφει το raw (string) cached value αν υπάρχει HIT, αλλιώς None
    (σε MISS *ή* αν το Redis είναι εκτός λειτουργίας).

    ΚΡΙΣΙΜΟ (requirement #3 του Feature C): αν το Redis είναι down, ΔΕΝ
    ρίχνουμε exception προς τα πάνω — απλά το αντιμετωπίζουμε σαν MISS και
    αφήνουμε τον caller να διαβάσει κανονικά από τη MySQL. Το caching είναι
    optimization, όχι hard dependency.
    """
    try:
        value = redis_client.get(key)
    except Exception as exc:
        logger.error(f"Redis GET απέτυχε για key={key}: {exc} — συνεχίζουμε χωρίς cache")
        return None

    if value is not None:
        logger.info(f"Cache HIT key={key}")
    else:
        logger.info(f"Cache MISS key={key}")
    return value


def cache_set(key: str, value: str, ttl_seconds: int) -> None:
    """
    Feature C — cache-aside, βήμα "write-through μετά από MISS".

    Το `ttl_seconds` αντιστοιχεί στα TTL που ορίζει η εκφώνηση: 30s για
    /terminals, 60s για /statistics/*. Αν το Redis είναι down, καταγράφουμε
    error αλλά ΔΕΝ ρίχνουμε exception — το response προς τον client έχει
    ήδη υπολογιστεί σωστά από τη MySQL, απλά δεν προλαβαίνει να μπει στο cache.
    """
    try:
        redis_client.setex(key, ttl_seconds, value)
    except Exception as exc:
        logger.error(f"Redis SET απέτυχε για key={key}: {exc} — συνεχίζουμε χωρίς cache")


def invalidate_cache() -> None:
    """
    Feature C — requirement #2: "Σε κάθε write endpoint, καθαρίστε
    ΟΛΟΚΛΗΡΟ το cache πριν επιστρέψετε το response".

    Καλείται από: /flag, /unflag, /decommission, /terminals/from-template.

    Χρησιμοποιούμε SCAN (όχι KEYS — το KEYS μπλοκάρει ολόκληρο το Redis σε
    μεγάλα datasets) πάνω στο δικό μας namespace "cache:*", ώστε να μην
    πειράξουμε τυχόν άλλα keys που θα μπορούσε να έχει το ίδιο Redis
    instance στο μέλλον.
    """
    try:
        deleted = 0
        for key in redis_client.scan_iter(f"{CACHE_KEY_PREFIX}*"):
            redis_client.delete(key)
            deleted += 1
        logger.info(f"Cache invalidated ({deleted} keys διαγράφηκαν)")
    except Exception as exc:
        # requirement #3: το Redis-down δεν πρέπει να ρίξει το write
        # request — η MySQL είναι ήδη ενημερωμένη (source of truth).
        logger.error(f"Cache invalidation απέτυχε: {exc} — συνεχίζουμε")


# ----------------------------------------------------------------------------
# 6) "Safe" / idempotent schema migrations (τρέχουν στο startup)
# ----------------------------------------------------------------------------
def ensure_column(table: str, column: str, ddl_type: str) -> None:
    """
    Προσθέτει μια στήλη σε έναν πίνακα, ΜΟΝΟ αν δεν υπάρχει ήδη — γενική,
    επαναχρησιμοποιήσιμη εκδοχή του idempotent migration που ζητάει ρητά η
    εκφώνηση στο Feature A4 (για το `terminals.updated_on`), εφαρμοσμένη
    και στο `hardware_family` (Feature B/D) που χρειαζόμαστε σε δύο πίνακες.

    Η MySQL (σε αντίθεση με τη MariaDB) ΔΕΝ υποστηρίζει
    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, οπότε ελέγχουμε πρώτα το
    INFORMATION_SCHEMA.COLUMNS και κάνουμε ALTER TABLE μόνο αν χρειάζεται.
    Έτσι το app μπορεί να ξεκινήσει (ή να κάνει restart) απεριόριστες
    φορές χωρίς να σκάει με "Duplicate column name" error — και δουλεύει
    είτε η στήλη λείπει εντελώς από το επίσημο schema, είτε υπάρχει ήδη.

    table:    όνομα πίνακα (π.χ. "terminals")
    column:   όνομα νέας στήλης (π.χ. "hardware_family")
    ddl_type: το SQL type/constraints της νέας στήλης (π.χ. "DATETIME NULL")
    """
    check_sql = text(
        """
        SELECT COUNT(*) AS cnt
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = :schema
          AND TABLE_NAME = :table_name
          AND COLUMN_NAME = :column_name
        """
    )
    # Το table/column name ΔΕΝ μπορεί να γίνει bind parameter σε DDL
    # (ALTER TABLE) στη MySQL — μόνο VALUES μπορούν να γίνουν parameterized.
    # Είναι ασφαλές εδώ γιατί τα ονόματα προέρχονται ΜΟΝΟ από τον δικό μας
    # κώδικα (σταθερά strings πιο κάτω), ΠΟΤΕ από user input.
    alter_sql = text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    with engine.begin() as conn:
        exists = conn.execute(check_sql, {"schema": DB_NAME, "table_name": table, "column_name": column}).scalar()
        if exists == 0:
            conn.execute(alter_sql)
            logger.info(f"Column {table}.{column} δεν υπήρχε — προστέθηκε.")
        else:
            logger.info(f"Column {table}.{column} υπάρχει ήδη — καμία αλλαγή.")


def ensure_decommission_queue_table() -> None:
    """
    Δημιουργεί τον πίνακα `decommission_queue` αν δεν υπάρχει ήδη.

    Εδώ το `CREATE TABLE IF NOT EXISTS` αρκεί (σε αντίθεση με το ADD
    COLUMN, αυτό ΥΠΟΣΤΗΡΙΖΕΤΑΙ κανονικά από τη MySQL).

    Δημιουργούμε αυτόν τον πίνακα από τον ΚΩΔΙΚΑ (και όχι μέσα στο
    01_schema.sql) επίτηδες: η εκφώνηση λέει ρητά "τον φτιάχνετε εσείς",
    και τα επίσημα seed/schema αρχεία (που θα δοθούν από το bootcamp)
    πιθανότατα δεν θα τον περιλαμβάνουν — οπότε το app πρέπει να μπορεί να
    τον δημιουργήσει μόνο του, idempotent, ανεξάρτητα από ποια εκδοχή του
    01_schema.sql χρησιμοποιείται.

    Στήλες:
      tid          -> primary key ΚΑΙ foreign key -> terminals.tid
      queued_on    -> πότε μπήκε το terminal στην ουρά decommission
      delete_after -> queued_on + 3 μέρες (υπολογίζεται στο A5 endpoint)
    """
    create_sql = text(
        """
        CREATE TABLE IF NOT EXISTS decommission_queue (
            tid          VARCHAR(20) NOT NULL PRIMARY KEY,
            queued_on    DATETIME    NOT NULL,
            delete_after DATETIME    NOT NULL,
            CONSTRAINT fk_decomm_tid FOREIGN KEY (tid) REFERENCES terminals(tid)
        )
        """
    )
    with engine.begin() as conn:
        conn.execute(create_sql)
    logger.info("Table decommission_queue: OK (δημιουργήθηκε ή υπήρχε ήδη).")


def run_startup_migrations() -> None:
    """Τρέχει όλα τα idempotent migrations πριν σηκωθεί το Flask app."""
    try:
        # Μόνο το updated_on λείπει πραγματικά από το επίσημο schema (το
        # hardware_family υπάρχει ήδη εκ κατασκευής και στο terminals και
        # στο templates — βλ. db/init/01_schema.sql — οπότε δεν χρειάζεται
        # idempotent προσθήκη γι' αυτό).
        ensure_column("terminals", "updated_on", "DATETIME NULL")
        ensure_decommission_queue_table()
    except SQLAlchemyError as exc:
        # Fail fast: προτιμάμε να σκάσει το app ΚΑΤΑ ΤΟ startup παρά να
        # ξεκινήσει "μισό" και να αποτυγχάνει αργότερα, μυστηριωδώς, σε
        # κάθε request.
        logger.error(f"Startup migration failed: {exc}")
        raise


# ----------------------------------------------------------------------------
# 7) Health check — technical requirement #5
# ----------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    """
    GET /health

    Ελέγχει ΞΕΧΩΡΙΣΤΑ MySQL και Redis (ένα από τα δύο μπορεί να είναι πάνω
    ενώ το άλλο κάτω — π.χ. αν έπεσε μόνο το Redis, δεν σημαίνει ότι έπεσε
    και η βάση). Επιστρέφει:
      - 200, αν ΚΑΙ τα δύο components είναι healthy
      - 503, αν έστω ένα είναι degraded
    μαζί με λεπτομέρεια ανά component, ώστε να είναι εύκολο το debugging
    (π.χ. σε monitoring/alerting).
    """
    status = {"database": "unknown", "redis": "unknown"}
    healthy = True

    # --- MySQL check ---
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        status["database"] = "ok"
    except Exception as exc:
        logger.error(f"Health check - database degraded: {exc}")
        status["database"] = "degraded"
        healthy = False

    # --- Redis check ---
    try:
        redis_client.ping()
        status["redis"] = "ok"
    except Exception as exc:
        logger.error(f"Health check - redis degraded: {exc}")
        status["redis"] = "degraded"
        healthy = False

    http_status = 200 if healthy else 503
    return jsonify({
        "status": "ok" if healthy else "degraded",
        "components": status,
    }), http_status


# ----------------------------------------------------------------------------
# 8) Feature A — Terminals
# ----------------------------------------------------------------------------

def row_to_terminal_dict(row) -> dict:
    """
    Μετατρέπει μια γραμμή αποτελέσματος (SQLAlchemy Row) σε dict, στη
    μορφή που ζητάει η εκφώνηση: `enabled` ως boolean, `last_call` ως ISO
    8601 string (ή None αν είναι NULL).

    Κοινή helper function για A1 και A3, ώστε να μην επαναλαμβάνουμε τη
    ίδια λογική μετατροπής σε δύο σημεία (DRY).
    """
    return {
        "tid": row.tid,
        "mid": row.mid,
        "hardware_model": row.hardware_model,
        "software_version": row.software_version,
        "enabled": bool(row.enabled),
        "last_call": row.last_call.isoformat() if row.last_call else None,
    }


TERMINALS_LIST_CACHE_TTL = 30  # δευτερόλεπτα — όπως ορίζει το Feature C


@app.route("/terminals", methods=["GET"])
def list_terminals():
    """
    A1. GET /terminals
        GET /terminals?enabled=true
        GET /terminals?enabled=false

    Επιστρέφει λίστα terminals, προαιρετικά φιλτραρισμένη βάσει `enabled`.

    Feature C — cache-aside pattern (TTL 30s):
      1. Υπολογίζουμε ένα cache key που περιλαμβάνει το φίλτρο (γιατί
         "/terminals" και "/terminals?enabled=true" έχουν διαφορετικό
         αποτέλεσμα — δεν μπορούν να μοιράζονται το ίδιο cached value).
      2. Αν υπάρχει HIT, επιστρέφουμε κατευθείαν το cached JSON χωρίς να
         αγγίξουμε τη MySQL.
      3. Αν MISS (ή το Redis είναι down), διαβάζουμε κανονικά από τη MySQL
         και μετά γράφουμε το αποτέλεσμα στο cache για την επόμενη φορά.
    """
    enabled_param = request.args.get("enabled")  # None, "true" ή "false" (raw string)

    # ΣΗΜΕΙΩΣΗ σχήματος: το terminals ΔΕΝ έχει στήλη `mid` απευθείας — έχει
    # `merchant_id` (foreign key -> merchants.id). Το `mid` (το business
    # κωδικό, π.χ. "MID000101") ζει στο merchants, οπότε κάνουμε πάντα JOIN
    # για να το φέρουμε. Επίσης η στήλη λέγεται `last_call_stamp` στο
    # πραγματικό schema — την κάνουμε alias σε `last_call` ώστε το JSON
    # output (και το row_to_terminal_dict) να μη χρειάζεται καμία αλλαγή.
    base_query = """
        SELECT t.tid, m.mid AS mid, t.hardware_model, t.software_version,
               t.enabled, t.last_call_stamp AS last_call
        FROM terminals t
        JOIN merchants m ON m.id = t.merchant_id
    """
    params = {}
    # Το ίδιο string χρησιμοποιείται και ως μέρος του cache key, ώστε κάθε
    # distinct φίλτρο (χωρίς φίλτρο / true / false) να έχει το δικό του entry.
    filter_label = "all"

    if enabled_param is not None:
        normalized = enabled_param.strip().lower()
        if normalized not in ("true", "false"):
            # Δεν αγνοούμε σιωπηλά άκυρη τιμή (θα ήταν παραπλανητικό — ο
            # χρήστης θα νόμιζε ότι το φίλτρο εφαρμόστηκε). Το κάνουμε 400.
            return jsonify({"error": "invalid 'enabled' query param, expected true/false"}), 400
        base_query += " WHERE t.enabled = :enabled_value"
        params["enabled_value"] = 1 if normalized == "true" else 0
        filter_label = normalized

    cache_key = f"{CACHE_KEY_PREFIX}terminals:{filter_label}"

    cached = cache_get(cache_key)
    if cached is not None:
        # Το cached value είναι ήδη ένα έτοιμο JSON array (string) — το
        # επιστρέφουμε ως έχει, χωρίς να χρειαστεί re-serialization.
        return app.response_class(cached, mimetype="application/json"), 200

    try:
        with engine.connect() as conn:
            # text() + bind parameters (:enabled_value) -> technical
            # requirement #3: ΠΟΤΕ f-string/concatenation μέσα σε SQL με
            # user input, ακόμα κι αν εδώ το input είναι απλά "true"/"false".
            result = conn.execute(text(base_query), params)
            terminals = [row_to_terminal_dict(row) for row in result]

        payload = json.dumps(terminals)
        cache_set(cache_key, payload, TERMINALS_LIST_CACHE_TTL)
        return app.response_class(payload, mimetype="application/json"), 200
    except SQLAlchemyError as exc:
        logger.error(f"list_terminals - database error: {exc}")
        return jsonify({"error": "database error"}), 500


@app.route("/terminals/<tid>", methods=["GET"])
def get_terminal(tid):
    """
    A2. GET /terminals/<tid>

    Επιστρέφει όλα τα σχετικά πεδία ενός terminal. 404 αν το tid δεν υπάρχει.
    """
    query = text(
        """
        SELECT t.tid, m.mid AS mid, t.hardware_model, t.software_version, t.enabled,
               t.last_call_stamp AS last_call, t.scenario_number, t.updated_on
        FROM terminals t
        JOIN merchants m ON m.id = t.merchant_id
        WHERE t.tid = :tid
        """
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(query, {"tid": tid}).fetchone()

        if row is None:
            return jsonify({"error": "terminal not found"}), 404

        return jsonify({
            "tid": row.tid,
            "mid": row.mid,
            "hardware_model": row.hardware_model,
            "software_version": row.software_version,
            "enabled": bool(row.enabled),
            "last_call": row.last_call.isoformat() if row.last_call else None,
            "scenario_number": row.scenario_number,
            "updated_on": row.updated_on.isoformat() if row.updated_on else None,
        }), 200
    except SQLAlchemyError as exc:
        logger.error(f"get_terminal({tid}) - database error: {exc}")
        return jsonify({"error": "database error"}), 500


@app.route("/terminals/flagged", methods=["GET"])
def list_flagged_terminals():
    """
    A3. GET /terminals/flagged

    Terminals όπου το scenario_number ΔΕΝ είναι NULL, κενό string, ή '0'.

    ΣΧΟΛΙΟ για τη σειρά των routes: το path εδώ είναι static ("flagged"),
    όχι μεταβλητό, οπότε ο Flask router το ξεχωρίζει σωστά από το
    "/terminals/<tid>" ανεξαρτήτως της σειράς δήλωσης στο αρχείο — δεν
    υπάρχει το κλασικό πρόβλημα "static route vs dynamic route" που
    συναντάμε σε άλλα frameworks/routers.
    """
    query = text(
        """
        SELECT t.tid, m.mid AS mid, t.hardware_model, t.software_version,
               t.enabled, t.last_call_stamp AS last_call
        FROM terminals t
        JOIN merchants m ON m.id = t.merchant_id
        WHERE t.scenario_number IS NOT NULL
          AND t.scenario_number != ''
          AND t.scenario_number != '0'
        """
    )
    try:
        with engine.connect() as conn:
            result = conn.execute(query)
            terminals = [row_to_terminal_dict(row) for row in result]
        return jsonify(terminals), 200
    except SQLAlchemyError as exc:
        logger.error(f"list_flagged_terminals - database error: {exc}")
        return jsonify({"error": "database error"}), 500


@app.route("/terminals/<tid>/flag", methods=["POST"])
def flag_terminal(tid):
    """
    A4 (flag). POST /terminals/<tid>/flag
    Body: { "scenario_number": "5" }

    Requirements:
      - 404 αν δεν υπάρχει το tid.
      - 400 αν λείπει το scenario_number από το body.
      - logger.info() για την αλλαγή (tid, παλιά τιμή, νέα τιμή).
      - invalidate cache μετά το write.

    Χρησιμοποιούμε engine.begin() (και όχι engine.connect()) ώστε το
    SELECT+UPDATE να τρέξουν μέσα στο ΙΔΙΟ transaction — αποφεύγουμε ένα
    (μικρό αλλά υπαρκτό) race condition όπου δύο ταυτόχυρα requests θα
    μπορούσαν να διαβάσουν το ίδιο "old value" πριν προλάβει το ένα να
    κάνει commit.
    """
    body = request.get_json(silent=True) or {}
    new_scenario = body.get("scenario_number")

    if new_scenario is None:
        return jsonify({"error": "scenario_number is required"}), 400
    # Το κρατάμε πάντα ως string, όπως ζητάει ρητά η εκφώνηση (ακόμα κι αν
    # ο client στείλει π.χ. αριθμό 5 αντί για "5").
    new_scenario = str(new_scenario)

    select_sql = text("SELECT scenario_number FROM terminals WHERE tid = :tid")
    update_sql = text(
        """
        UPDATE terminals
        SET scenario_number = :new_value, updated_on = :now
        WHERE tid = :tid
        """
    )

    try:
        with engine.begin() as conn:
            row = conn.execute(select_sql, {"tid": tid}).fetchone()
            if row is None:
                return jsonify({"error": "terminal not found"}), 404

            old_scenario = row.scenario_number
            now = datetime.utcnow()
            conn.execute(update_sql, {"new_value": new_scenario, "now": now, "tid": tid})

        logger.info(f"FLAG tid={tid} scenario_number: '{old_scenario}' -> '{new_scenario}'")
        invalidate_cache()
        return jsonify({"tid": tid, "scenario_number": new_scenario}), 200
    except SQLAlchemyError as exc:
        logger.error(f"flag_terminal({tid}) - database error: {exc}")
        return jsonify({"error": "database error"}), 500


@app.route("/terminals/<tid>/unflag", methods=["POST"])
def unflag_terminal(tid):
    """
    A4 (unflag). POST /terminals/<tid>/unflag

    Θέτει scenario_number = '0'. Ίδια δομή/λογική με το flag_terminal(),
    απλά χωρίς να χρειάζεται body.
    """
    select_sql = text("SELECT scenario_number FROM terminals WHERE tid = :tid")
    update_sql = text(
        """
        UPDATE terminals
        SET scenario_number = '0', updated_on = :now
        WHERE tid = :tid
        """
    )
    try:
        with engine.begin() as conn:
            row = conn.execute(select_sql, {"tid": tid}).fetchone()
            if row is None:
                return jsonify({"error": "terminal not found"}), 404

            old_scenario = row.scenario_number
            now = datetime.utcnow()
            conn.execute(update_sql, {"now": now, "tid": tid})

        logger.info(f"UNFLAG tid={tid} scenario_number: '{old_scenario}' -> '0'")
        invalidate_cache()
        return jsonify({"tid": tid, "scenario_number": "0"}), 200
    except SQLAlchemyError as exc:
        logger.error(f"unflag_terminal({tid}) - database error: {exc}")
        return jsonify({"error": "database error"}), 500


@app.route("/terminals/<tid>/decommission", methods=["POST"])
def decommission_terminal(tid):
    """
    A5. POST /terminals/<tid>/decommission

    1. Ελέγχει ότι το terminal υπάρχει ΚΑΙ είναι enabled=1.
       Αν είναι ήδη decommissioned (enabled=0) -> 409 Conflict.
    2. Θέτει enabled = 0.
    3. Καταχωρεί το tid στο decommission_queue με
       queued_on = now, delete_after = now + 3 μέρες.

    Το SELECT + UPDATE + INSERT τρέχουν ΟΛΑ μέσα στο ΙΔΙΟ transaction
    (engine.begin()): είτε θα γίνουν ΚΑΙ τα δύο write (UPDATE terminals ΚΑΙ
    INSERT στο decommission_queue), είτε ΚΑΝΕΝΑ. Δεν θέλουμε ποτέ ένα
    terminal να καταλήξει με enabled=0 χωρίς αντίστοιχο entry στην ουρά
    (ή το αντίστροφο) αν κάτι αποτύχει στη μέση.
    """
    select_sql = text("SELECT enabled FROM terminals WHERE tid = :tid")
    update_sql = text("UPDATE terminals SET enabled = 0, updated_on = :now WHERE tid = :tid")
    insert_queue_sql = text(
        """
        INSERT INTO decommission_queue (tid, queued_on, delete_after)
        VALUES (:tid, :queued_on, :delete_after)
        """
    )

    try:
        with engine.begin() as conn:
            row = conn.execute(select_sql, {"tid": tid}).fetchone()
            if row is None:
                return jsonify({"error": "terminal not found"}), 404
            if row.enabled == 0:
                return jsonify({"error": "terminal already decommissioned"}), 409

            now = datetime.utcnow()
            delete_after = now + timedelta(days=3)

            conn.execute(update_sql, {"now": now, "tid": tid})
            conn.execute(insert_queue_sql, {"tid": tid, "queued_on": now, "delete_after": delete_after})

        logger.info(
            f"DECOMMISSION tid={tid} queued_on={now.isoformat()} delete_after={delete_after.isoformat()}"
        )
        invalidate_cache()
        return jsonify({
            "tid": tid,
            "queued_on": now.isoformat(),
            "delete_after": delete_after.isoformat(),
        }), 200
    except SQLAlchemyError as exc:
        logger.error(f"decommission_terminal({tid}) - database error: {exc}")
        return jsonify({"error": "database error"}), 500


@app.route("/terminals/decommissioned", methods=["GET"])
def list_decommissioned():
    """
    Βοηθητικό endpoint (αναφέρεται στην εκφώνηση, κάτω από το A5).
    GET /terminals/decommissioned

    Terminals στην ουρά διαγραφής, με πόσες μέρες απομένουν μέχρι το
    delete_after. Το `days_remaining` μπορεί να είναι και αρνητικό, αν το
    delete_after έχει ήδη περάσει αλλά δεν έχει τρέξει ακόμα το nightly
    cleanup cron job (bonus feature) που θα το αφαιρέσει οριστικά.
    """
    query = text(
        """
        SELECT dq.tid, dq.queued_on, dq.delete_after,
               t.hardware_model, m.mid AS mid
        FROM decommission_queue dq
        JOIN terminals t ON t.tid = dq.tid
        JOIN merchants m ON m.id = t.merchant_id
        ORDER BY dq.delete_after ASC
        """
    )
    try:
        with engine.connect() as conn:
            result = conn.execute(query)
            now = datetime.utcnow()
            items = []
            for row in result:
                days_left = (row.delete_after - now).total_seconds() / 86400
                items.append({
                    "tid": row.tid,
                    "mid": row.mid,
                    "hardware_model": row.hardware_model,
                    "queued_on": row.queued_on.isoformat(),
                    "delete_after": row.delete_after.isoformat(),
                    "days_remaining": round(days_left, 2),
                })
        return jsonify(items), 200
    except SQLAlchemyError as exc:
        logger.error(f"list_decommissioned - database error: {exc}")
        return jsonify({"error": "database error"}), 500


# ----------------------------------------------------------------------------
# 9) Feature B — Templates
# ----------------------------------------------------------------------------

def row_to_template_dict(row) -> dict:
    """
    Κοινή helper για B1/B2.

    ΣΗΜΕΙΩΣΗ σχήματος: το επίσημο templates έχει PK στήλη `id` (όχι
    `template_id`) και δεν έχει καθόλου `software_version`/`description` —
    έχει αντ' αυτού `template_name`. Το SQL κάνει alias `id AS template_id`
    ώστε το JSON API contract (`template_id`) να μείνει σταθερό ανεξάρτητα
    από το πώς λέγεται η στήλη στη βάση.
    """
    return {
        "template_id": row.template_id,
        "template_name": row.template_name,
        "hardware_model": row.hardware_model,
        "hardware_family": row.hardware_family,
    }


@app.route("/templates", methods=["GET"])
def list_templates():
    """
    B1. GET /templates

    Λίστα όλων των διαθέσιμων templates.
    """
    query = text(
        """
        SELECT id AS template_id, template_name, hardware_model, hardware_family
        FROM templates
        ORDER BY id
        """
    )
    try:
        with engine.connect() as conn:
            result = conn.execute(query)
            templates = [row_to_template_dict(row) for row in result]
        return jsonify(templates), 200
    except SQLAlchemyError as exc:
        logger.error(f"list_templates - database error: {exc}")
        return jsonify({"error": "database error"}), 500


@app.route("/templates/<int:template_id>", methods=["GET"])
def get_template(template_id):
    """
    B2. GET /templates/<id>

    Λεπτομέρειες ενός template. 404 αν δεν υπάρχει.

    Σημείωση: χρησιμοποιούμε τον Flask converter <int:template_id> — αν το
    path segment δεν είναι έγκυρος ακέραιος (π.χ. /templates/abc), η Flask
    επιστρέφει αυτόματα 404 πριν καν μπει στο route handler, κάτι απόλυτα
    συνεπές με το "404 αν δεν υπάρχει" της εκφώνησης. Στη βάση, το route
    param αντιστοιχεί στη στήλη `id` (η πραγματική PK του templates).
    """
    query = text(
        """
        SELECT id AS template_id, template_name, hardware_model, hardware_family
        FROM templates
        WHERE id = :template_id
        """
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(query, {"template_id": template_id}).fetchone()

        if row is None:
            return jsonify({"error": "template not found"}), 404

        return jsonify(row_to_template_dict(row)), 200
    except SQLAlchemyError as exc:
        logger.error(f"get_template({template_id}) - database error: {exc}")
        return jsonify({"error": "database error"}), 500


def compute_next_tid(existing_tids: list, mid: str) -> str:
    """
    "Καθαρή" (χωρίς I/O) λογική υπολογισμού του επόμενου tid — δεν αγγίζει
    ΚΑΘΟΛΟΥ τη βάση, παίρνει έτοιμη τη λίστα υπαρχόντων tids ως όρισμα. Έτσι
    μπορεί να γίνει unit-tested χωρίς MySQL (βλ. app/tests/test_main.py).

    Κανόνας από την εκφώνηση (Feature B3, βήμα 3): "βρείτε το TID με το
    μεγαλύτερο αριθμητικό suffix για τον ίδιο merchant" — π.χ. αν υπάρχουν
    T0101001..T0101004, το πρόθεμα είναι T0101 και ο επόμενος αριθμός
    είναι 005 -> T0101005.

    ΥΠΟΘΕΣΗ (η εκφώνηση δεν καλύπτει ρητά αυτή την περίπτωση): αν ο
    merchant δεν έχει ΚΑΝΕΝΑ terminal ακόμα, δεν υπάρχει προηγούμενο TID
    από το οποίο να "διαβάσουμε" το πρόθεμα. Παρατηρώντας το seed data
    (π.χ. mid=MID000101 -> terminals T0101xxx, mid=MID000102 ->
    T0102xxx), το πρόθεμα φαίνεται να παράγεται ντετερμινιστικά από το mid:
    "T" + τα τελευταία 4 ψηφία του mid. Το χρησιμοποιούμε ΜΟΝΟ ως fallback
    για τον πρώτο terminal ενός merchant.
    """
    if existing_tids:
        # Το πρόθεμα είναι ό,τι μένει αν αφαιρέσουμε τα τελευταία 3 ψηφία
        # (τον αριθμητικό μετρητή) — υποθέτουμε ότι ΟΛΑ τα terminals ενός
        # merchant μοιράζονται το ίδιο πρόθεμα, όπως δείχνει το παράδειγμα.
        # Παίρνουμε το ΜΕΓΙΣΤΟ suffix (όχι το πλήθος), ώστε να δουλεύει
        # σωστά ακόμα κι αν λείπει κάποιος ενδιάμεσος αριθμός (π.χ. αν ένα
        # terminal διαγράφηκε στο παρελθόν).
        prefix = existing_tids[0][:-3]
        max_suffix = max(int(tid[-3:]) for tid in existing_tids)
        next_suffix = max_suffix + 1
    else:
        prefix = f"T{mid[-4:]}"
        next_suffix = 1

    return f"{prefix}{next_suffix:03d}"


def generate_next_tid(conn, mid: str) -> str:
    """
    "Βρώμικη" (I/O) πλευρά: διαβάζει τα υπάρχοντα tids του merchant από τη
    βάση, και αναθέτει τον πραγματικό υπολογισμό στο compute_next_tid().

    ΣΗΜΕΙΩΣΗ σχήματος: το terminals ΔΕΝ έχει στήλη `mid` — μόνο
    `merchant_id` (FK -> merchants.id). Άρα για να βρούμε "όλα τα tids
    αυτού του merchant" κάνουμε JOIN μέσω merchants.mid.

    Πρέπει να καλείται ΜΕΣΑ σε transaction (ίδιο `conn` με το INSERT που θα
    ακολουθήσει στο create_terminal_from_template), ώστε το SELECT-max-και-
    INSERT να είναι atomic και να μην υπάρχει race condition αν δύο
    requests έρθουν ταυτόχρονα για τον ίδιο mid.
    """
    existing_tids = conn.execute(
        text(
            """
            SELECT t.tid
            FROM terminals t
            JOIN merchants m ON m.id = t.merchant_id
            WHERE m.mid = :mid
            """
        ),
        {"mid": mid},
    ).scalars().all()

    return compute_next_tid(existing_tids, mid)


@app.route("/terminals/from-template", methods=["POST"])
def create_terminal_from_template():
    """
    B3. POST /terminals/from-template
    Body: { "template_id": 1, "mid": "MID000101" }

    1. Ελέγχει ότι το template υπάρχει (404 αν όχι).
    2. Ελέγχει ότι το mid υπάρχει στο merchants (404 αν όχι) — και παίρνει
       το ΠΡΑΓΜΑΤΙΚΟ (surrogate) merchants.id, γιατί αυτό χρειάζεται το
       terminals.merchant_id (foreign key), όχι το business "mid" string.
    3. Υπολογίζει νέο, μοναδικό tid (generate_next_tid).
    4. Εισάγει νέα γραμμή στο terminals, αντιγράφοντας hardware_model και
       hardware_family από το template (τα ΜΟΝΑ πεδία που έχει το
       templates schema — δεν υπάρχει software_version σε αυτό, οπότε το
       νέο terminal μένει με software_version = NULL μέχρι να ενημερωθεί
       αλλού, π.χ. στο πρώτο πραγματικό provisioning call).
    5. 201 Created με το νέο tid.

    Όλα τα βήματα 3-4 τρέχουν ΜΕΣΑ σε ΜΙΑ transaction (engine.begin()),
    όπως ζητάει ρητά η εκφώνηση — αν οτιδήποτε αποτύχει στη μέση, ΤΙΠΟΤΑ
    δεν γράφεται στη βάση (ούτε "μισό" terminal, ούτε λάθος tid).

    Feature C: επειδή είναι write endpoint, καθαρίζουμε ΟΛΟΚΛΗΡΟ το cache
    πριν επιστρέψουμε response (ίδια λογική με flag/unflag/decommission).
    """
    body = request.get_json(silent=True) or {}
    template_id = body.get("template_id")
    mid = body.get("mid")

    if template_id is None or mid is None:
        return jsonify({"error": "template_id and mid are required"}), 400

    select_template_sql = text(
        "SELECT hardware_model, hardware_family FROM templates WHERE id = :template_id"
    )
    # Χρειαζόμαστε το merchants.id (surrogate PK) — ΟΧΙ μόνο επιβεβαίωση ότι
    # υπάρχει το mid — γιατί αυτό είναι η τιμή που θα μπει στο
    # terminals.merchant_id (foreign key).
    select_merchant_sql = text("SELECT id FROM merchants WHERE mid = :mid")
    insert_terminal_sql = text(
        """
        INSERT INTO terminals
            (tid, merchant_id, template_id, hardware_model, hardware_family,
             enabled, last_call_stamp, scenario_number, updated_on)
        VALUES
            (:tid, :merchant_id, :template_id, :hardware_model, :hardware_family,
             1, NULL, NULL, :now)
        """
    )

    try:
        with engine.begin() as conn:
            template_row = conn.execute(select_template_sql, {"template_id": template_id}).fetchone()
            if template_row is None:
                return jsonify({"error": "template not found"}), 404

            merchant_row = conn.execute(select_merchant_sql, {"mid": mid}).fetchone()
            if merchant_row is None:
                return jsonify({"error": "merchant not found"}), 404
            merchant_pk = merchant_row.id

            new_tid = generate_next_tid(conn, mid)
            now = datetime.utcnow()

            conn.execute(insert_terminal_sql, {
                "tid": new_tid,
                "merchant_id": merchant_pk,
                "template_id": template_id,
                "hardware_model": template_row.hardware_model,
                "hardware_family": template_row.hardware_family,
                "now": now,
            })

        logger.info(f"CREATE-FROM-TEMPLATE tid={new_tid} mid={mid} template_id={template_id}")
        invalidate_cache()
        return jsonify({
            "tid": new_tid,
            "mid": mid,
            "hardware_model": template_row.hardware_model,
            "hardware_family": template_row.hardware_family,
        }), 201
    except SQLAlchemyError as exc:
        logger.error(f"create_terminal_from_template - database error: {exc}")
        return jsonify({"error": "database error"}), 500


# ----------------------------------------------------------------------------
# 10) Feature D — Στατιστικά με Pandas
# ----------------------------------------------------------------------------

STATISTICS_CACHE_TTL = 60  # δευτερόλεπτα — όπως ορίζει το Feature D/C


def load_terminals_dataframe() -> pd.DataFrame:
    """
    Φορτώνει ΟΛΑ τα terminals σε ένα pandas DataFrame, για χρήση από τα
    D1-D4. Κοινή helper ώστε κάθε endpoint να μην ξαναγράφει το ίδιο
    `pd.read_sql(...)`.

    ΣΗΜΕΙΩΣΗ: δεν χρειάζεται JOIN με merchants εδώ — κανένα από τα D1-D4
    στατιστικά δεν ομαδοποιεί/φιλτράρει βάσει merchant, οπότε δεν
    χρειαζόμαστε το `mid`. Η στήλη `last_call_stamp` γίνεται alias σε
    `last_call` ώστε ο υπόλοιπος κώδικας (bucket_idle_days κ.λπ.) να μη
    χρειάζεται καμία αλλαγή.

    Χρησιμοποιούμε το SQLAlchemy engine απευθείας ως "connectable" στο
    pandas — το pandas κάνει από μόνο του τη διαχείριση connection/cursor.
    """
    query = text(
        "SELECT tid, hardware_model, hardware_family, enabled, last_call_stamp AS last_call FROM terminals"
    )
    return pd.read_sql(query, engine)


@app.route("/statistics/by-hardware", methods=["GET"])
def statistics_by_hardware():
    """
    D1. GET /statistics/by-hardware

    Κατανομή terminals ανά hardware_model (όλα τα terminals, ενεργά και
    ανενεργά — η εκφώνηση δεν ζητάει φίλτρο εδώ, το active/inactive split
    καλύπτεται ξεχωριστά από το D2).

    Feature C: cached με TTL 60s.
    """
    cache_key = f"{CACHE_KEY_PREFIX}statistics:by-hardware"
    cached = cache_get(cache_key)
    if cached is not None:
        return app.response_class(cached, mimetype="application/json"), 200

    try:
        df = load_terminals_dataframe()
        counts = df.groupby("hardware_model").size().reset_index(name="count")
        data = counts.to_dict(orient="records")

        payload = json.dumps({
            "generated_at": datetime.utcnow().isoformat(),
            "data": data,
        })
        cache_set(cache_key, payload, STATISTICS_CACHE_TTL)
        return app.response_class(payload, mimetype="application/json"), 200
    except Exception as exc:
        # Εδώ πιάνουμε γενικό Exception (όχι μόνο SQLAlchemyError) γιατί το
        # pandas μπορεί να ρίξει και δικά του exceptions (π.χ. σε άδειο
        # DataFrame ή απροσδόκητο τύπο δεδομένων) — technical requirement
        # #2: ποτέ silent failure, πάντα logger.error + 500.
        logger.error(f"statistics_by_hardware - error: {exc}")
        return jsonify({"error": "database error"}), 500


@app.route("/statistics/by-state", methods=["GET"])
def statistics_by_state():
    """
    D2. GET /statistics/by-state

    Πλήθος ενεργών (enabled=1) vs ανενεργών (enabled=0) terminals.
    Feature C: cached με TTL 60s.
    """
    cache_key = f"{CACHE_KEY_PREFIX}statistics:by-state"
    cached = cache_get(cache_key)
    if cached is not None:
        return app.response_class(cached, mimetype="application/json"), 200

    try:
        df = load_terminals_dataframe()
        active = int((df["enabled"] == 1).sum())
        inactive = int((df["enabled"] == 0).sum())

        payload = json.dumps({
            "generated_at": datetime.utcnow().isoformat(),
            "active": active,
            "inactive": inactive,
            "total": active + inactive,
        })
        cache_set(cache_key, payload, STATISTICS_CACHE_TTL)
        return app.response_class(payload, mimetype="application/json"), 200
    except Exception as exc:
        logger.error(f"statistics_by_state - error: {exc}")
        return jsonify({"error": "database error"}), 500


@app.route("/statistics/by-hardware-family", methods=["GET"])
def statistics_by_hardware_family():
    """
    D3. GET /statistics/by-hardware-family

    Ίδιο σχήμα με το D1, αλλά ομαδοποιημένο ανά hardware_family.

    ΣΗΜΕΙΩΣΗ: terminals που δεν έχουν (ακόμα) τιμή στο hardware_family
    (π.χ. παλιές γραμμές από πριν προστεθεί η στήλη — βλ. ensure_column())
    ομαδοποιούνται στην κατηγορία "Unknown", ώστε να μην εξαφανίζονται
    σιωπηλά από τα στατιστικά.
    """
    cache_key = f"{CACHE_KEY_PREFIX}statistics:by-hardware-family"
    cached = cache_get(cache_key)
    if cached is not None:
        return app.response_class(cached, mimetype="application/json"), 200

    try:
        df = load_terminals_dataframe()
        df["hardware_family"] = df["hardware_family"].fillna("Unknown")
        counts = df.groupby("hardware_family").size().reset_index(name="count")
        data = counts.to_dict(orient="records")

        payload = json.dumps({
            "generated_at": datetime.utcnow().isoformat(),
            "data": data,
        })
        cache_set(cache_key, payload, STATISTICS_CACHE_TTL)
        return app.response_class(payload, mimetype="application/json"), 200
    except Exception as exc:
        logger.error(f"statistics_by_hardware_family - error: {exc}")
        return jsonify({"error": "database error"}), 500


def bucket_idle_days(days) -> str:
    """
    Αντιστοιχίζει έναν αριθμό ημερών αδράνειας στο σωστό bucket, όπως
    ορίζει η εκφώνηση (Σήμερα / 1-7 / 8-30 / 31-90 / 90+).

    ΥΠΟΘΕΣΗ (δεν καλύπτεται ρητά στην εκφώνηση): terminals που ΔΕΝ έχουν
    ΠΟΤΕ κάνει last_call (NULL στη βάση) μπαίνουν σε ξεχωριστό bucket
    "Ποτέ", αντί να αγνοηθούν σιωπηλά ή να σκάσουν σε NaN υπολογισμούς.
    """
    if pd.isna(days):
        return "Ποτέ"
    if days <= 0:
        return "Σήμερα"
    if days <= 7:
        return "1-7 μέρες"
    if days <= 30:
        return "8-30 μέρες"
    if days <= 90:
        return "31-90 μέρες"
    return "90+ μέρες"


@app.route("/statistics/idle-distribution", methods=["GET"])
def statistics_idle_distribution():
    """
    D4. GET /statistics/idle-distribution

    Κατανομή terminals ανά ημέρες αδράνειας από το last_call_stamp μέχρι σήμερα.

    ΣΗΜΕΙΩΣΗ ονομασίας πεδίου: επιβεβαιώθηκε από το επίσημο schema ότι η
    πραγματική στήλη λέγεται `last_call_stamp` (όχι `last_call` όπως θα
    υπέθετε κανείς από το JSON output του Feature A) — το
    load_terminals_dataframe() κάνει ήδη το alias `AS last_call`, οπότε ο
    κώδικας εδώ παραμένει αμετάβλητος.

    Τα buckets εμφανίζονται με σταθερή, λογική σειρά (όχι αλφαβητική), και
    μόνο όσα έχουν count > 0.
    """
    cache_key = f"{CACHE_KEY_PREFIX}statistics:idle-distribution"
    cached = cache_get(cache_key)
    if cached is not None:
        return app.response_class(cached, mimetype="application/json"), 200

    try:
        df = load_terminals_dataframe()
        now = pd.Timestamp(datetime.utcnow())

        # last_call έρχεται ήδη ως pandas Timestamp (ή NaT αν είναι NULL)
        # χάρη στο pd.read_sql — δεν χρειάζεται χειροκίνητο parsing.
        idle_days = (now - df["last_call"]).dt.days
        buckets = idle_days.apply(bucket_idle_days)

        counts = buckets.value_counts()

        # Σταθερή σειρά εμφάνισης, ανεξάρτητα από τη σειρά που τα βρήκε το
        # pandas — έτσι το response είναι προβλέψιμο/συγκρίσιμο μεταξύ calls.
        bucket_order = ["Σήμερα", "1-7 μέρες", "8-30 μέρες", "31-90 μέρες", "90+ μέρες", "Ποτέ"]
        data = [
            {"range": bucket, "count": int(counts[bucket])}
            for bucket in bucket_order
            if bucket in counts.index
        ]

        payload = json.dumps({
            "generated_at": datetime.utcnow().isoformat(),
            "data": data,
        })
        cache_set(cache_key, payload, STATISTICS_CACHE_TTL)
        return app.response_class(payload, mimetype="application/json"), 200
    except Exception as exc:
        logger.error(f"statistics_idle_distribution - error: {exc}")
        return jsonify({"error": "database error"}), 500


# ----------------------------------------------------------------------------
# 11) Bonus — Daily CSV report
# ----------------------------------------------------------------------------
@app.route("/reports/terminals-basic", methods=["GET"])
def report_terminals_basic_csv():
    """
    Bonus: "Daily CSV report (endpoint που παράγει terminals_basic.csv)".

    Η εκφώνηση δεν καθορίζει ρητά το URL path — μόνο το όνομα του
    παραγόμενου αρχείου (terminals_basic.csv). Διαλέξαμε το
    GET /reports/terminals-basic ως λογικό, RESTful naming· το αρχείο που
    κατεβάζει ο client ονομάζεται ΠΑΝΤΑ terminals_basic.csv (μέσω του
    header Content-Disposition), ανεξάρτητα από το URL.

    Χρησιμοποιούμε pandas (ήδη dependency για το Feature D) για να
    μετατρέψουμε το query αποτέλεσμα σε CSV με μία γραμμή (`to_csv`), αντί
    να γράφουμε χειροκίνητα το built-in csv module.
    """
    query = text(
        """
        SELECT t.tid, m.mid AS mid, t.hardware_model, t.software_version,
               t.enabled, t.last_call_stamp AS last_call
        FROM terminals t
        JOIN merchants m ON m.id = t.merchant_id
        ORDER BY t.tid
        """
    )
    try:
        df = pd.read_sql(query, engine)
        # bool αντί για 0/1 -> πιο ευανάγνωστο αν το αρχείο ανοίξει σε Excel.
        df["enabled"] = df["enabled"].astype(bool)

        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)
        csv_content = csv_buffer.getvalue()

        logger.info(f"Παράχθηκε terminals_basic.csv report ({len(df)} γραμμές)")

        return Response(
            csv_content,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=terminals_basic.csv"},
        )
    except Exception as exc:
        # Γενικό Exception (όχι μόνο SQLAlchemyError) — το pandas/to_csv
        # μπορεί να ρίξει και δικά του exceptions. Technical requirement
        # #2: ποτέ silent failure.
        logger.error(f"report_terminals_basic_csv - error: {exc}")
        return jsonify({"error": "database error"}), 500


# ----------------------------------------------------------------------------
# 12) Generic fallback error handlers (defense-in-depth)
# ----------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(_e):
    # Καλύπτει paths που δεν ταιριάζουν με ΚΑΝΕΝΑ route (π.χ. typo στο URL).
    return jsonify({"error": "not found"}), 404


@app.errorhandler(500)
def internal_error(e):
    # "Δίχτυ ασφαλείας": αν κάποιο unexpected exception ξεφύγει από τα
    # try/except των endpoints, καταλήγει εδώ αντί να γυρίσει ένα ωμό
    # Flask/Werkzeug HTML error page στον client.
    logger.error(f"Unhandled 500: {e}")
    return jsonify({"error": "internal server error"}), 500


# ----------------------------------------------------------------------------
# 13) Entrypoint
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting TMS API...")
    run_startup_migrations()
    # host="0.0.0.0" -> ΥΠΟΧΡΕΩΤΙΚΟ μέσα σε container. Αν αφήναμε το default
    # (127.0.0.1), το Flask θα άκουγε ΜΟΝΟ μέσα στο δικό του network
    # namespace και δεν θα ήταν προσβάσιμο έξω από το container, ούτε καν
    # μέσω του port mapping στο docker-compose.yml.
    app.run(host="0.0.0.0", port=APP_PORT)

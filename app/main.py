"""
main.py — Terminal Management System (TMS) API
================================================

Entrypoint του Flask API. Εδώ:
  - Στήνουμε logging (ΠΑΝΤΑ σε stdout, με timestamp + level + message).
  - Δημιουργούμε connections προς MySQL (μέσω SQLAlchemy) και Redis.
  - Κάνουμε "safe" / idempotent migrations: προσθέτουμε τη στήλη
    `updated_on` στο terminals, και δημιουργούμε το `decommission_queue`.
  - Ορίζουμε τα endpoints του Feature A (A1-A5) + το γενικό /health.

Design decision: χρησιμοποιούμε το SQLAlchemy ΜΟΝΟ ως connection-pool
manager + query builder (Core API, με text() + bind parameters), ΟΧΙ ως
πλήρες ORM με model classes. Αυτό μας δίνει connection pooling και
parameterized queries (technical requirement #3) χωρίς την επιπλέον
πολυπλοκότητα ενός πλήρους ORM layer.

ΣΗΜΕΙΩΣΗ: Τα Feature B (templates), Feature C (Redis caching) και Feature D
(Pandas statistics) δεν έχουν ακόμα λεπτομερή εκφώνηση, οπότε δεν είναι
πλήρως υλοποιημένα εδώ. Υπάρχει ήδη ένα cache-invalidation "hook"
(`invalidate_terminal_cache`) έτοιμο να επεκταθεί μόλις δοθεί το Feature C.
"""

import os
import sys
import logging
from datetime import datetime, timedelta

from flask import Flask, jsonify, request
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
import redis
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


def invalidate_terminal_cache(tid: str) -> None:
    """
    Καθαρίζει (invalidate) οποιοδήποτε cached entry σχετίζεται με το
    συγκεκριμένο terminal, καθώς και οποιαδήποτε cached "λίστα" terminals
    (π.χ. αποτελέσματα από /terminals?enabled=true ή /terminals/flagged) —
    μια αλλαγή σε ένα terminal μπορεί να αλλάξει τα αποτελέσματα τέτοιων
    λιστών, οπότε δεν αρκεί να σβήσουμε μόνο το μεμονωμένο terminal.

    ΣΗΜΕΙΩΣΗ: η πλήρης στρατηγική caching (ποια ακριβώς keys, ποιο TTL,
    μέτρηση hit/miss) θα οριστικοποιηθεί στο Feature C, μόλις δοθεί η
    αναλυτική εκφώνησή του. Προς το παρόν, αυτή η function είναι το "hook"
    που καλείται μετά από κάθε write endpoint (A4, A5), όπως ζητάει ρητά η
    εκφώνηση ("Μετά από κάθε write, καθαρίστε το cache").
    """
    try:
        redis_client.delete(f"terminal:{tid}")
        for key in redis_client.scan_iter("terminals:list:*"):
            redis_client.delete(key)
        logger.info(f"Cache invalidated for tid={tid}")
    except Exception as exc:
        # Ένα πρόβλημα στο Redis ΔΕΝ πρέπει να ρίξει ολόκληρο το write
        # request — η MySQL είναι το "source of truth". Το καταγράφουμε
        # ως error για ορατότητα, αλλά η κύρια λειτουργία συνεχίζει.
        logger.error(f"Cache invalidation failed for tid={tid}: {exc}")


# ----------------------------------------------------------------------------
# 6) "Safe" / idempotent schema migrations (τρέχουν στο startup)
# ----------------------------------------------------------------------------
def ensure_updated_on_column() -> None:
    """
    Προσθέτει τη στήλη `updated_on` στο `terminals`, ΜΟΝΟ αν δεν υπάρχει ήδη.

    Η MySQL (σε αντίθεση με τη MariaDB) ΔΕΝ υποστηρίζει
    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, οπότε ελέγχουμε πρώτα το
    INFORMATION_SCHEMA.COLUMNS και κάνουμε ALTER TABLE μόνο αν χρειάζεται.
    Έτσι το app μπορεί να ξεκινήσει (ή να κάνει restart) απεριόριστες
    φορές χωρίς να σκάει με "Duplicate column name" error.
    """
    check_sql = text(
        """
        SELECT COUNT(*) AS cnt
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = :schema
          AND TABLE_NAME = 'terminals'
          AND COLUMN_NAME = 'updated_on'
        """
    )
    alter_sql = text("ALTER TABLE terminals ADD COLUMN updated_on DATETIME NULL")

    with engine.begin() as conn:
        exists = conn.execute(check_sql, {"schema": DB_NAME}).scalar()
        if exists == 0:
            conn.execute(alter_sql)
            logger.info("Column terminals.updated_on δεν υπήρχε — προστέθηκε.")
        else:
            logger.info("Column terminals.updated_on υπάρχει ήδη — καμία αλλαγή.")


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
        ensure_updated_on_column()
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


@app.route("/terminals", methods=["GET"])
def list_terminals():
    """
    A1. GET /terminals
        GET /terminals?enabled=true
        GET /terminals?enabled=false

    Επιστρέφει λίστα terminals, προαιρετικά φιλτραρισμένη βάσει `enabled`.
    """
    enabled_param = request.args.get("enabled")  # None, "true" ή "false" (raw string)

    base_query = """
        SELECT tid, mid, hardware_model, software_version, enabled, last_call
        FROM terminals
    """
    params = {}

    if enabled_param is not None:
        normalized = enabled_param.strip().lower()
        if normalized not in ("true", "false"):
            # Δεν αγνοούμε σιωπηλά άκυρη τιμή (θα ήταν παραπλανητικό — ο
            # χρήστης θα νόμιζε ότι το φίλτρο εφαρμόστηκε). Το κάνουμε 400.
            return jsonify({"error": "invalid 'enabled' query param, expected true/false"}), 400
        base_query += " WHERE enabled = :enabled_value"
        params["enabled_value"] = 1 if normalized == "true" else 0

    try:
        with engine.connect() as conn:
            # text() + bind parameters (:enabled_value) -> technical
            # requirement #3: ΠΟΤΕ f-string/concatenation μέσα σε SQL με
            # user input, ακόμα κι αν εδώ το input είναι απλά "true"/"false".
            result = conn.execute(text(base_query), params)
            terminals = [row_to_terminal_dict(row) for row in result]
        return jsonify(terminals), 200
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
        SELECT tid, mid, hardware_model, software_version, enabled,
               last_call, scenario_number, updated_on
        FROM terminals
        WHERE tid = :tid
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
        SELECT tid, mid, hardware_model, software_version, enabled, last_call
        FROM terminals
        WHERE scenario_number IS NOT NULL
          AND scenario_number != ''
          AND scenario_number != '0'
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
        invalidate_terminal_cache(tid)
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
        invalidate_terminal_cache(tid)
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
        invalidate_terminal_cache(tid)
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
               t.hardware_model, t.mid
        FROM decommission_queue dq
        JOIN terminals t ON t.tid = dq.tid
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
# 9) Generic fallback error handlers (defense-in-depth)
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
# 10) Entrypoint
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting TMS API...")
    run_startup_migrations()
    # host="0.0.0.0" -> ΥΠΟΧΡΕΩΤΙΚΟ μέσα σε container. Αν αφήναμε το default
    # (127.0.0.1), το Flask θα άκουγε ΜΟΝΟ μέσα στο δικό του network
    # namespace και δεν θα ήταν προσβάσιμο έξω από το container, ούτε καν
    # μέσω του port mapping στο docker-compose.yml.
    app.run(host="0.0.0.0", port=APP_PORT)

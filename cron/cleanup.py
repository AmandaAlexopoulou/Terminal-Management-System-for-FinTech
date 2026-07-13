"""
cleanup.py — Bonus: Nightly Decommission Cleanup
==================================================

Ξεχωριστό, μακρόβιο (long-running) process που τρέχει ΜΙΑ φορά την ημέρα
(default: 02:00 UTC — configurable) και οριστικά διαγράφει από τη βάση όσα
terminals έχουν περάσει το `delete_after` τους στο decommission_queue.

Γιατί ΟΧΙ system cron daemon μέσα στο container: το κλασικό Unix `cron`
είναι δύσκολο να στηθεί σωστά σε containers — θέλει να τρέχει ως daemon σε
background (συγκρούεται με το "1 foreground process per container" pattern
του Docker), τα logs του πηγαίνουν by default σε syslog αντί για stdout
(σπάει το technical requirement #1 — logs ΠΑΝΤΑ σε stdout), και οι
μεταβλητές περιβάλλοντος (DB_HOST κ.λπ.) δεν περνάνε αυτόματα στο
crontab χωρίς επιπλέον "τρικ". Αντ' αυτού: ένα απλό Python process που
υπολογίζει πόσο πρέπει να κοιμηθεί μέχρι την επόμενη προγραμματισμένη
εκτέλεση, κάνει sleep, τρέχει το cleanup, και επαναλαμβάνει επ' άπειρον.
Πιο απλό, πιο διαφανές στα logs, και ευκολότερο να τεσταριστεί.
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text, bindparam
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# Environment variables — ίδιο pattern με το app/main.py, ώστε να μοιράζεται
# το ίδιο .env / docker-compose environment block.
# ----------------------------------------------------------------------------
load_dotenv()

DB_HOST = os.getenv("DB_HOST", "mysql")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("MYSQL_DATABASE")
DB_USER = os.getenv("MYSQL_USER")
DB_PASSWORD = os.getenv("MYSQL_PASSWORD")

# Ώρα (UTC) στην οποία τρέχει το nightly cleanup. Configurable μέσω env
# vars ώστε να μπορεί κανείς να την αλλάξει χωρίς rebuild του image.
CRON_HOUR = int(os.getenv("CRON_HOUR", "2"))
CRON_MINUTE = int(os.getenv("CRON_MINUTE", "0"))

# Βολικό για demo/testing: αν True, τρέχει το cleanup ΜΙΑ φορά αμέσως στο
# startup (επιπλέον του κανονικού nightly schedule) — έτσι δεν χρειάζεται
# να περιμένει κανείς 24ωρο για να δει ότι δουλεύει το container.
RUN_ON_STARTUP = os.getenv("CLEANUP_RUN_ON_STARTUP", "false").strip().lower() == "true"

# ----------------------------------------------------------------------------
# Logging — ΠΑΝΤΑ σε stdout, ίδιο format με το main.py (technical requirement #1).
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("tms-cron")

# ----------------------------------------------------------------------------
# Database engine
# ----------------------------------------------------------------------------
DB_URL = f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=280)


def run_cleanup() -> None:
    """
    Διαγράφει terminals με delete_after < NOW() από το decommission_queue
    ΚΑΙ από το terminals.

    ΣΕΙΡΑ ΔΙΑΓΡΑΦΗΣ (το "πιθανό εμπόδιο" που επισημαίνει η εκφώνηση): το
    decommission_queue.tid έχει FOREIGN KEY REFERENCES terminals(tid) —
    δηλαδή το decommission_queue είναι το "child" table και το terminals
    είναι το "parent". Η MySQL ΔΕΝ επιτρέπει να διαγράψεις μια parent
    γραμμή όσο υπάρχει ακόμα child γραμμή που δείχνει σε αυτήν (foreign
    key constraint error). Άρα ΠΡΩΤΑ διαγράφουμε από το decommission_queue
    (child), ΜΕΤΑ από το terminals (parent) — ποτέ αντίστροφα.

    Όλο το operation τρέχει μέσα σε ΜΙΑ transaction (engine.begin()): είτε
    διαγράφονται ΚΑΙ τα δύο σύνολα γραμμών, είτε ΚΑΝΕΝΑ — δεν θέλουμε ποτέ
    ένα "μισό" cleanup (π.χ. να σβήσει το decommission_queue entry αλλά όχι
    το terminal, αφήνοντάς το ορφανό αλλά ακόμα υπαρκτό στη βάση).
    """
    select_expired_sql = text(
        "SELECT tid FROM decommission_queue WHERE delete_after < :now"
    )
    delete_queue_sql = text(
        "DELETE FROM decommission_queue WHERE delete_after < :now"
    )
    # expanding=True: επιτρέπει στο SQLAlchemy να μετατρέψει μια λίστα
    # Python σε ένα σωστά-parameterized "IN (:tid_0, :tid_1, ...)" — ΠΑΝΤΑ
    # bind parameters, ποτέ f-string/concatenation (technical requirement #3),
    # ακόμα και για μεταβλητού μήκους λίστες.
    delete_terminals_sql = text(
        "DELETE FROM terminals WHERE tid IN :tids"
    ).bindparams(bindparam("tids", expanding=True))

    try:
        with engine.begin() as conn:
            now = datetime.utcnow()
            expired_tids = conn.execute(select_expired_sql, {"now": now}).scalars().all()

            if not expired_tids:
                logger.info("Cleanup: κανένα terminal δεν έχει περάσει το delete_after — καμία ενέργεια.")
                return

            # 1) ΠΡΩΤΑ το child table (decommission_queue) ...
            conn.execute(delete_queue_sql, {"now": now})
            # 2) ... ΜΕΤΑ το parent table (terminals). Αν κάναμε την
            # αντίστροφη σειρά, θα σκάγαμε με FK constraint error.
            conn.execute(delete_terminals_sql, {"tids": expired_tids})

        logger.info(f"Cleanup: διαγράφηκαν {len(expired_tids)} terminals: {expired_tids}")
    except SQLAlchemyError as exc:
        # requirement: ποτέ silent failure — logger.error, αλλά ΔΕΝ ρίχνουμε
        # exception προς τα πάνω. Το process πρέπει να ΣΥΝΕΧΙΣΕΙ να ζει και
        # να ξαναδοκιμάσει στο επόμενο προγραμματισμένο run, αντί να πεθάνει
        # (οπότε ολόκληρο το nightly cleanup θα σταματούσε να τρέχει ΓΙΑ ΠΑΝΤΑ
        # μέχρι χειροκίνητο restart).
        logger.error(f"Cleanup απέτυχε: {exc}")


def seconds_until_next_run() -> float:
    """
    Υπολογίζει πόσα δευτερόλεπτα απομένουν μέχρι την επόμενη προγραμματισμένη
    εκτέλεση (CRON_HOUR:CRON_MINUTE UTC — σήμερα, ή αύριο αν η ώρα αυτή έχει
    ήδη περάσει για σήμερα).
    """
    now = datetime.now(timezone.utc)
    next_run = now.replace(hour=CRON_HOUR, minute=CRON_MINUTE, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


if __name__ == "__main__":
    logger.info(f"tms-cron ξεκίνησε. Nightly cleanup στις {CRON_HOUR:02d}:{CRON_MINUTE:02d} UTC.")

    if RUN_ON_STARTUP:
        logger.info("CLEANUP_RUN_ON_STARTUP=true — τρέχω ένα άμεσο cleanup τώρα (για testing/demo).")
        run_cleanup()

    # Άπειρο loop: sleep μέχρι την επόμενη προγραμματισμένη ώρα, run, repeat.
    # Αυτό ΕΙΝΑΙ το "foreground process" του container — όσο τρέχει αυτό το
    # loop, το container παραμένει "up" (`docker compose ps` το δείχνει).
    while True:
        sleep_seconds = seconds_until_next_run()
        logger.info(f"Επόμενο cleanup σε {sleep_seconds / 3600:.2f} ώρες.")
        time.sleep(sleep_seconds)
        run_cleanup()

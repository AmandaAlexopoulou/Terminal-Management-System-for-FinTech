"""
test_main.py — Unit tests (Bonus requirement: pytest, ≥3 functions)

Καλύπτουμε 4 "καθαρές" functions του main.py — καθεμία δεν κάνει I/O
(καμία database ή Redis κλήση), οπότε τα tests τρέχουν γρήγορα και δεν
χρειάζονται containers up. Οι functions με I/O (π.χ. list_terminals,
flag_terminal) θα χρειάζονταν integration tests με πραγματική/mock βάση —
εκτός scope για αυτό το bonus requirement.

Εκτέλεση:
    cd app
    pip install -r requirements.txt -r requirements-dev.txt
    pytest tests/ -v

Ή μέσα στο ήδη τρέχον container:
    docker compose exec tms-api pytest tests/ -v
"""

import types
from datetime import datetime

import pandas as pd

from main import (
    bucket_idle_days,
    compute_next_tid,
    row_to_terminal_dict,
    row_to_template_dict,
)


def make_row(**kwargs):
    """
    Βοηθητική factory: SQLAlchemy Row objects υποστηρίζουν attribute access
    (π.χ. row.tid) — ένα types.SimpleNamespace έχει ακριβώς την ίδια
    διεπαφή, οπότε είναι ένα ελαφρύ, χωρίς-εξαρτήσεις test double.
    """
    return types.SimpleNamespace(**kwargs)


# ============================================================================
# bucket_idle_days (Feature D4)
# ============================================================================
class TestBucketIdleDays:
    def test_zero_days_is_today(self):
        assert bucket_idle_days(0) == "Σήμερα"

    def test_negative_days_is_today(self):
        # Οριακή περίπτωση: last_call "στο μέλλον" (π.χ. clock skew) — δεν
        # πρέπει να σκάσει, αντιμετωπίζεται σαν "Σήμερα".
        assert bucket_idle_days(-1) == "Σήμερα"

    def test_one_to_seven_days(self):
        assert bucket_idle_days(1) == "1-7 μέρες"
        assert bucket_idle_days(7) == "1-7 μέρες"

    def test_eight_to_thirty_days(self):
        assert bucket_idle_days(8) == "8-30 μέρες"
        assert bucket_idle_days(30) == "8-30 μέρες"

    def test_thirty_one_to_ninety_days(self):
        assert bucket_idle_days(31) == "31-90 μέρες"
        assert bucket_idle_days(90) == "31-90 μέρες"

    def test_over_ninety_days(self):
        assert bucket_idle_days(91) == "90+ μέρες"
        assert bucket_idle_days(500) == "90+ μέρες"

    def test_nan_means_never_called(self):
        # terminals με last_call = NULL στη βάση καταλήγουν ως NaN μετά το
        # pandas datetime subtraction — πρέπει να μπαίνουν στο bucket "Ποτέ".
        assert bucket_idle_days(float("nan")) == "Ποτέ"
        assert bucket_idle_days(pd.NA) == "Ποτέ"


# ============================================================================
# compute_next_tid (Feature B3)
# ============================================================================
class TestComputeNextTid:
    def test_example_from_spec(self):
        # Το ακριβές παράδειγμα της εκφώνησης: T0101001..T0101004 -> T0101005
        existing = ["T0101001", "T0101002", "T0101003", "T0101004"]
        assert compute_next_tid(existing, "MID000101") == "T0101005"

    def test_first_terminal_for_new_merchant(self):
        # Merchant χωρίς κανένα terminal ακόμα -> fallback πρόθεμα από το mid.
        assert compute_next_tid([], "MID000199") == "T0199001"

    def test_uses_max_suffix_not_count(self):
        # Αν λείπει ένας ενδιάμεσος αριθμός (π.χ. διαγράφηκε παλιότερα το
        # T0101002/003), πρέπει να πάρει max+1 (=005), ΟΧΙ count+1 (=003).
        existing = ["T0101001", "T0101004"]
        assert compute_next_tid(existing, "MID000101") == "T0101005"

    def test_single_existing_terminal(self):
        assert compute_next_tid(["T0102001"], "MID000102") == "T0102002"

    def test_zero_padding_stays_three_digits(self):
        existing = [f"T0101{n:03d}" for n in range(1, 10)]  # ...008, 009
        assert compute_next_tid(existing, "MID000101") == "T0101010"


# ============================================================================
# row_to_terminal_dict (Feature A1/A3)
# ============================================================================
class TestRowToTerminalDict:
    def test_enabled_int_becomes_bool(self):
        row = make_row(
            tid="T0101001", mid="MID000101", hardware_model="Desk2600",
            software_version="12.4.0", enabled=1, last_call=None,
        )
        result = row_to_terminal_dict(row)
        assert result["enabled"] is True
        assert result["last_call"] is None

    def test_disabled_int_becomes_bool_false(self):
        row = make_row(
            tid="T0103001", mid="MID000103", hardware_model="Lane3000",
            software_version="9.9.9", enabled=0, last_call=None,
        )
        result = row_to_terminal_dict(row)
        assert result["enabled"] is False

    def test_last_call_formatted_as_iso8601(self):
        dt = datetime(2026, 6, 23, 12, 35, 56)
        row = make_row(
            tid="T0101001", mid="MID000101", hardware_model="Desk2600",
            software_version="12.4.0", enabled=1, last_call=dt,
        )
        result = row_to_terminal_dict(row)
        assert result["last_call"] == "2026-06-23T12:35:56"

    def test_all_fields_present(self):
        row = make_row(
            tid="T1", mid="M1", hardware_model="X",
            software_version="1.0", enabled=1, last_call=None,
        )
        result = row_to_terminal_dict(row)
        assert set(result.keys()) == {
            "tid", "mid", "hardware_model", "software_version", "enabled", "last_call",
        }


# ============================================================================
# row_to_template_dict (Feature B1/B2)
# ============================================================================
class TestRowToTemplateDict:
    def test_basic_mapping(self):
        row = make_row(
            template_id=1, hardware_model="Desk2600", hardware_family="Desktop",
            software_version="12.4.0", description="Standard countertop terminal",
        )
        result = row_to_template_dict(row)
        assert result == {
            "template_id": 1,
            "hardware_model": "Desk2600",
            "hardware_family": "Desktop",
            "software_version": "12.4.0",
            "description": "Standard countertop terminal",
        }

    def test_null_hardware_family_passes_through_as_none(self):
        # Παλιά template γραμμή (πριν προστεθεί η στήλη hardware_family) —
        # δεν πρέπει να σκάσει, απλά περνάει None.
        row = make_row(
            template_id=2, hardware_model="Move5000", hardware_family=None,
            software_version="10.1.2", description=None,
        )
        result = row_to_template_dict(row)
        assert result["hardware_family"] is None
        assert result["description"] is None

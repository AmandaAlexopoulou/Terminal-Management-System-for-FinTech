"""
conftest.py — pytest configuration για το app/tests/.

Δεν χρησιμοποιούμε "src layout" ή package installation (θα ήταν
υπερβολικό για το μέγεθος αυτού του project) — απλά προσθέτουμε το
γονικό φάκελο (app/) στο sys.path, ώστε το `from main import ...` στα
test files να δουλεύει είτε το pytest τρέξει από το app/, είτε από το
tests/, είτε από το project root.
"""

import os
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

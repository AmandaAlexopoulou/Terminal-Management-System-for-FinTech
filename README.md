# Terminal Management System (TMS)

REST API για διαχείριση ενός fleet από POS terminals. Flask + MySQL + Redis,
όλα μέσα σε Docker Compose.

> ⚠️ **Κατάσταση εργασίας.** Υλοποιημένα: **Μέρος 0-1 (setup/Docker/DB)**,
> **Feature A** (terminals, A1-A5), **Feature B** (templates, B1-B3),
> **Feature C** (Redis cache-aside + invalidation, ενσωματωμένο μέσα στα A1
> και D1-D4) και **Feature D** (στατιστικά με Pandas, D1-D4), μαζί με όλες
> τις γενικές τεχνικές απαιτήσεις (`/health`, logging σε stdout, error
> handling, parameterized queries, secrets σε `.env`).
>
> **Bonus — και τα 4 υλοποιημένα:**
> - Cron job (`tms-cron` container) για nightly cleanup του `decommission_queue`
> - Daily CSV report: `GET /reports/terminals-basic`
> - Unit tests (pytest): 18 tests σε 4 functions, `app/tests/`
> - `/health` per service: `tms-api` έχει HTTP `/health`· `mysql`/`redis`
>   έχουν Docker-level `healthcheck:` (ο καθιερωμένος τρόπος για off-the-shelf
>   images χωρίς δικό μας HTTP layer)
>
> Τα `db/init/01_schema.sql` και `db/init/02_seed.sql` είναι **placeholder**
> αρχεία (βασισμένα στα πεδία που περιγράφει η εκφώνηση — π.χ. πρόσθεσα
> `hardware_family` και έκανα το `templates.template_id` αριθμητικό
> auto-increment, ώστε να ταιριάζει με το παράδειγμα body του B3), γιατί τα
> πραγματικά "θα δοθούν" από το bootcamp. **Αντικαταστήστε τα με τα επίσημα
> πριν την τελική παράδοση** — αν το επίσημο schema έχει διαφορετικά
> ονόματα/τύπους στηλών, θα χρειαστούν μικρές προσαρμογές στο `main.py`.

## Οδηγίες εκκίνησης

1. Αντιγράψτε το `.env.example` σε `.env` και συμπληρώστε πραγματικά
   passwords (ΜΗΝ το ανεβάσετε ποτέ στο git):
   ```bash
   cp .env.example .env
   ```
2. Σηκώστε όλο το σύστημα:
   ```bash
   docker compose up --build
   ```
   Ξεκινάει 4 containers: `tms-mysql`, `tms-redis`, `tms-api`, `tms-cron`
   (bonus). Το `tms-api` και το `tms-cron` περιμένουν (`depends_on` με
   `condition: service_healthy`) τα απαιτούμενα services να είναι έτοιμα
   πριν ξεκινήσουν.
3. Το API είναι διαθέσιμο στο `http://localhost:5000`.
4. Δοκιμή:
   ```bash
   curl http://localhost:5000/health
   curl http://localhost:5000/terminals
   ```
5. Τερματισμός:
   ```bash
   docker compose down          # κρατάει τα δεδομένα (named volume)
   docker compose down -v       # διαγράφει και το volume (καθαρό restart)
   ```

## Endpoints

| Method | Path                             | Περιγραφή                                                          |
|--------|-----------------------------------|------------------------------------------------------------------------|
| GET    | `/health`                         | Health check MySQL + Redis ξεχωριστά. `200` αν όλα ok, αλλιώς `503`.  |
| GET    | `/terminals`                      | Λίστα όλων των terminals (cached, TTL 30s).                            |
| GET    | `/terminals?enabled=true`         | Μόνο ενεργά terminals (cached, TTL 30s).                                |
| GET    | `/terminals?enabled=false`        | Μόνο ανενεργά terminals (cached, TTL 30s).                              |
| GET    | `/terminals/<tid>`                | Λεπτομέρειες ενός terminal. `404` αν δεν υπάρχει.                     |
| GET    | `/terminals/flagged`              | Terminals με `scenario_number` διάφορο NULL / `''` / `'0'`.           |
| POST   | `/terminals/<tid>/flag`           | Θέτει `scenario_number` (body: `{"scenario_number": "5"}`). `400`/`404`. |
| POST   | `/terminals/<tid>/unflag`         | Θέτει `scenario_number = '0'`.                                         |
| POST   | `/terminals/<tid>/decommission`   | `enabled = 0` + entry στο `decommission_queue` (3 μέρες). `409` αν ήδη decommissioned. |
| GET    | `/terminals/decommissioned`       | Terminals στην ουρά διαγραφής + πόσες μέρες απομένουν.                |
| GET    | `/templates`                      | Λίστα όλων των templates.                                              |
| GET    | `/templates/<id>`                 | Λεπτομέρειες ενός template. `404` αν δεν υπάρχει.                     |
| POST   | `/terminals/from-template`        | Δημιουργεί terminal από template (body: `{"template_id": 1, "mid": "..."}`). `201`. |
| GET    | `/statistics/by-hardware`         | Κατανομή terminals ανά `hardware_model` (cached, TTL 60s).            |
| GET    | `/statistics/by-state`            | Πλήθος ενεργών/ανενεργών terminals (cached, TTL 60s).                 |
| GET    | `/statistics/by-hardware-family`  | Κατανομή terminals ανά `hardware_family` (cached, TTL 60s).           |
| GET    | `/statistics/idle-distribution`   | Κατανομή terminals ανά μέρες αδράνειας (cached, TTL 60s).             |
| GET    | `/reports/terminals-basic`        | [Bonus] Κατεβάζει `terminals_basic.csv` με βασικά στοιχεία terminals. |

## Δομή project

```
tms/
├── docker-compose.yml
├── .env.example
├── README.md
├── app/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── requirements-dev.txt   (μόνο pytest — για τοπικά unit tests)
│   ├── main.py
│   └── tests/
│       ├── conftest.py
│       └── test_main.py
├── cron/                       (Bonus: nightly decommission cleanup)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── cleanup.py
├── scripts/
│   └── smoke_test.ps1          (αυτοματοποιημένο end-to-end test, όλα τα features)
└── db/
    └── init/
        ├── 01_schema.sql   (placeholder — αντικαταστήστε με το επίσημο)
        └── 02_seed.sql     (placeholder — αντικαταστήστε με το επίσημο)
```

## Πλήρες End-to-End Test (προτεινόμενο πρώτο βήμα)

Αντί να τρέχετε endpoints ένα-ένα χειροκίνητα, το `scripts/smoke_test.ps1`
περνάει από **όλα** τα features (A, B, C, D + τα 3 bonus) σε μία εκτέλεση,
με καθαρό OK/FAIL output ανά βήμα:

```powershell
docker compose up --build -d   # -d: στο background, ώστε να μείνει ελεύθερο το terminal
.\scripts\smoke_test.ps1
```

Αν δείτε `PSSecurityException` / "cannot be loaded because running scripts
is disabled", επιτρέψτε την εκτέλεση **μόνο για αυτό το process** (δεν
αλλάζει μόνιμες ρυθμίσεις ασφαλείας):
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\smoke_test.ps1
```

Το script είναι ασφαλές να ξανατρέξει πολλές φορές — κάθε φορά δημιουργεί
ΝΕΟ terminal μέσω `from-template`, οπότε δεν συγκρούεται με terminals από
προηγούμενα runs.

> **Σημείωση:** το script είναι σκόπιμα γραμμένο σε καθαρά αγγλικά (σχόλια
> και μηνύματα), όχι επειδή "δεν επιτρέπονται" ελληνικά σε PowerShell
> scripts, αλλά για να αποφευχθεί ένα γνωστό πρόβλημα: η Windows
> PowerShell 5.1 (η built-in, όχι το PowerShell 7) διαβάζει `.ps1` αρχεία
> χωρίς UTF-8 BOM χρησιμοποιώντας το codepage των Windows, πράγμα που
> «σπάει» πολυ-byte ελληνικούς χαρακτήρες και μπορεί να προκαλέσει
> parse errors. Αγγλικά μόνο = δουλεύει παντού, ανεξαρτήτως έκδοσης
> PowerShell/codepage.

## Επιβεβαίωση caching (Feature C)

Όπως ζητάει η εκφώνηση, ελέγξτε HIT/MISS στα logs:

```bash
curl http://localhost:5000/terminals   # -> στα logs: Cache MISS
curl http://localhost:5000/terminals   # -> στα logs: Cache HIT (μέσα σε 30s)

curl -X POST http://localhost:5000/terminals/T0101001/flag \
     -H "Content-Type: application/json" -d '{"scenario_number": "9"}'

curl http://localhost:5000/terminals   # -> ξανά Cache MISS (invalidated μετά το write)
```

## Unit Tests (Bonus)

18 tests πάνω σε 4 "καθαρές" (χωρίς I/O) functions: `bucket_idle_days`,
`compute_next_tid`, `row_to_terminal_dict`, `row_to_template_dict`. Δεν
χρειάζονται MySQL/Redis up για να τρέξουν.

**Τοπικά:**
```bash
cd app
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v
```

**Μέσα στο ήδη τρέχον container:**
```bash
docker compose exec tms-api pytest tests/ -v
```

## Daily CSV Report (Bonus)

```bash
curl -o terminals_basic.csv http://localhost:5000/reports/terminals-basic
```
Κατεβάζει ένα CSV με `tid, mid, hardware_model, software_version, enabled,
last_call` για όλα τα terminals — έτοιμο να ανοιχτεί σε Excel.

## Cron Job — Nightly Decommission Cleanup (Bonus)

Το `tms-cron` container τρέχει από μόνο του κάθε βράδυ (default: 02:00 UTC,
configurable μέσω `CRON_HOUR`/`CRON_MINUTE` στο `.env`) και διαγράφει
οριστικά terminals με `delete_after < NOW()`, με τη σωστή σειρά (πρώτα
`decommission_queue`, μετά `terminals`, λόγω του foreign key).

**Για να το δείτε να δουλεύει χωρίς να περιμένετε 24 ώρες**, βάλτε στο
`.env`:
```
CLEANUP_RUN_ON_STARTUP=true
```
και κάντε `docker compose up --build` — θα δείτε στα logs του `tms-cron`
ένα άμεσο cleanup run κατά το startup. Ελέγξτε τα logs με:
```bash
docker compose logs -f tms-cron
```

## Παράδειγμα Ροής Δοκιμών (Feature B, A4, A5)

Ενδεικτική αλληλουχία calls (από πραγματικό τρέξιμο σε PowerShell) που
επιβεβαιώνει ότι Feature B, A4 και A5 δουλεύουν σωστά μαζί με το
transaction/idempotency logic τους.

**1. Δημιουργία terminal από template (Feature B3):**
```powershell
Invoke-RestMethod -Uri http://localhost:5000/terminals/from-template -Method Post -ContentType "application/json" -Body '{"template_id": 1, "mid": "MID000101"}'
```
```
hardware_family  : Desktop
hardware_model   : Desk2600
mid              : MID000101
software_version : 12.4.0
tid              : T0101003
```
Το νέο `tid` (`T0101003`) υπολογίστηκε σωστά ως "επόμενος αριθμός" μετά τα
υπάρχοντα `T0101001`/`T0101002` του ίδιου merchant.

**2. Flag (Feature A4):**
```powershell
Invoke-RestMethod -Uri http://localhost:5000/terminals/T0101001/flag -Method Post -ContentType "application/json" -Body '{"scenario_number": "9"}'
```
```
scenario_number tid
--------------- ---
9               T0101001
```

**3. Unflag (Feature A4):**
```powershell
Invoke-RestMethod -Uri http://localhost:5000/terminals/T0101001/unflag -Method Post
```
```
scenario_number tid
--------------- ---
0               T0101001
```

**4. Decommission (Feature A5):**
```powershell
Invoke-RestMethod -Uri http://localhost:5000/terminals/T0101001/decommission -Method Post
```
```
delete_after                queued_on                   tid
------------                ---------                   ---
2026-07-16T07:03:01.861045  2026-07-13T07:03:01.861045  T0101001
```
`delete_after` = `queued_on` + 3 μέρες, όπως ζητάει η εκφώνηση.

**5. Δεύτερο decommission στο ίδιο terminal → σωστά αποτυγχάνει με 409:**
```powershell
Invoke-RestMethod -Uri http://localhost:5000/terminals/T0101001/decommission -Method Post
```
```
Invoke-RestMethod : {"error":"terminal already decommissioned"}
```
Αναμενόμενη συμπεριφορά — επιβεβαιώνει το duplicate-decommission guard.

## Σημειώσεις σχεδίασης

- Το SQLAlchemy χρησιμοποιείται ως connection-pool manager + query builder
  (`text()` με bind parameters), όχι ως πλήρες ORM με model classes — αρκεί
  για τις ανάγκες της εργασίας και κρατάει τον κώδικα απλό.
- Η στήλη `terminals.updated_on` και ο πίνακας `decommission_queue`
  δημιουργούνται **idempotent** από τον ίδιο τον κώδικα στο startup
  (`run_startup_migrations()` στο `app/main.py`), όχι μέσα στα `.sql` seed
  αρχεία — έτσι το app «αυτο-επουλώνεται» ανεξάρτητα από το ποια εκδοχή του
  επίσημου schema θα χρησιμοποιηθεί τελικά.
- Το `/terminals/<tid>/decommission` κάνει `UPDATE` + `INSERT` μέσα στο ίδιο
  transaction (`engine.begin()`), ώστε να μην υπάρχει ποτέ ασυνεπής
  κατάσταση (terminal disabled χωρίς entry στην ουρά, ή αντίστροφα). Το ίδιο
  ισχύει και για το `/terminals/from-template` (υπολογισμός νέου `tid` +
  `INSERT` στο terminals, atomic).
- Το idempotent column-adding της A4 (`ensure_column()`) γενικεύτηκε ώστε
  να καλύπτει και τη νέα στήλη `hardware_family` (terminals + templates),
  αντί να γράψουμε ξεχωριστό, σχεδόν πανομοιότυπο κώδικα για κάθε στήλη.
- Το Feature C (cache-aside) υλοποιείται με δύο μικρές, επαναχρησιμοποιήσιμες
  helper functions (`cache_get`/`cache_set`) που «καταπίνουν» σιωπηλά
  οποιοδήποτε σφάλμα Redis (log + συνέχεια χωρίς cache) — έτσι το ίδιο
  pattern εφαρμόζεται με μία γραμμή κώδικα σε κάθε cached endpoint
  (`/terminals`, και τα 4 `/statistics/*`), χωρίς επανάληψη try/except
  παντού.
- Το νέο `tid` στο Feature B3 υπολογίζεται με απλή αριθμητική στο πρόθεμα
  του υπάρχοντος μεγαλύτερου TID για τον ίδιο merchant. Αν ο merchant δεν
  έχει ακόμα κανένα terminal, χρησιμοποιείται ένα fallback πρόθεμα
  βασισμένο στο `mid` (βλ. σχόλιο στο `generate_next_tid()` στο `main.py`
  — η εκφώνηση δεν καλύπτει ρητά αυτή την περίπτωση).
- Το `tms-cron` container ΔΕΝ χρησιμοποιεί system `cron` daemon — απλό
  Python process με ένα `while True` loop που υπολογίζει πόσο να κοιμηθεί
  μέχρι την επόμενη προγραμματισμένη ώρα. Πιο απλό σε containers (χωρίς τα
  γνωστά προβλήματα του cron daemon με foreground process/stdout logging/
  env vars), και το ίδιο script τρέχει τη διαγραφή με τη σωστή σειρά
  (`decommission_queue` πρώτα, `terminals` μετά) μέσα σε μία transaction.
- Η "καθαρή" λογική του `compute_next_tid()` αποσπάστηκε από το
  `generate_next_tid()` ειδικά ώστε να γίνεται unit-tested χωρίς βάση
  δεδομένων — το ίδιο pattern (διαχωρισμός I/O από λογική) θα μπορούσε να
  εφαρμοστεί και σε άλλα σημεία αν χρειαστεί μεγαλύτερη test coverage.

-- ============================================================================
-- 01_schema.sql — TMS database schema
-- ============================================================================
-- ⚠️ ΠΡΟΣΩΡΙΝΟ / PLACEHOLDER ⚠️
--
-- Η εκφώνηση αναφέρει ρητά ότι αυτό το αρχείο "θα σας δοθεί" από το
-- bootcamp μαζί με το 02_seed.sql. Δεν το είχαμε διαθέσιμο, οπότε φτιάξαμε
-- μια λογική εκδοχή βασισμένη ΑΠΟΚΛΕΙΣΤΙΚΑ στα πεδία που περιγράφει η
-- εκφώνηση (tid, mid, hardware_model, software_version, enabled, last_call,
-- scenario_number για το terminals· mid για το merchants· κ.λπ.) — ώστε να
-- μπορείτε να τρέξετε και να τεστάρετε ΤΩΡΑ ολόκληρο το σύστημα.
--
-- ΑΝΤΙΚΑΤΑΣΤΗΣΤΕ αυτό το αρχείο (και το 02_seed.sql) με τα επίσημα, μόλις
-- σας δοθούν, ΠΡΙΝ την τελική παράδοση — οι ακριβείς ονομασίες/τύποι
-- στηλών του πραγματικού schema μπορεί να διαφέρουν ελαφρώς.
-- ============================================================================

CREATE TABLE IF NOT EXISTS merchants (
    mid           VARCHAR(20)  NOT NULL PRIMARY KEY,
    business_name VARCHAR(120) NOT NULL,
    city          VARCHAR(80),
    created_on    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS terminals (
    tid              VARCHAR(20)  NOT NULL PRIMARY KEY,
    mid              VARCHAR(20)  NOT NULL,
    hardware_model   VARCHAR(60)  NOT NULL,
    software_version VARCHAR(30)  NOT NULL,
    enabled          TINYINT(1)   NOT NULL DEFAULT 1,
    last_call        DATETIME     NULL,
    scenario_number  VARCHAR(10)  NULL,
    -- Σκόπιμα ΔΕΝ ορίζουμε εδώ τη στήλη `updated_on`: η εκφώνηση (Feature
    -- A4) ζητάει ρητά να προστεθεί ΑΠΟ ΤΟΝ ΚΩΔΙΚΑ, με idempotent
    -- ALTER TABLE, ως άσκηση σε "safe migrations" — βλ. main.py,
    -- ensure_updated_on_column().
    CONSTRAINT fk_terminals_mid FOREIGN KEY (mid) REFERENCES merchants(mid)
);

CREATE TABLE IF NOT EXISTS templates (
    template_id      VARCHAR(20)  NOT NULL PRIMARY KEY,
    hardware_model   VARCHAR(60)  NOT NULL,
    software_version VARCHAR(30)  NOT NULL,
    description       VARCHAR(200)
);

-- Σημείωση: ο πίνακας `decommission_queue` (Feature A5) ΔΕΝ δημιουργείται
-- εδώ επίτηδες — δημιουργείται idempotent από το ίδιο το app στο startup
-- (main.py), γιατί η εκφώνηση λέει ρητά "τον φτιάχνετε εσείς" και το
-- επίσημο schema piece που θα δοθεί μάλλον δεν θα τον περιλαμβάνει.

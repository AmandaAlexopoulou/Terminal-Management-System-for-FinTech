-- ============================================================================
-- 02_seed.sql — Δείγμα δεδομένων
-- ============================================================================
-- ⚠️ ΠΡΟΣΩΡΙΝΟ / PLACEHOLDER (βλ. σχόλιο στο 01_schema.sql) ⚠️
-- Αντικαταστήστε με το επίσημο seed αρχείο μόλις σας δοθεί.
-- ============================================================================

INSERT INTO merchants (mid, business_name, city) VALUES
    ('MID000101', 'Kafeneio To Steki', 'Athens'),
    ('MID000102', 'SuperMarket Lemonis', 'Thessaloniki'),
    ('MID000103', 'Bakery Ouranos', 'Patras');

INSERT INTO terminals (tid, mid, hardware_model, hardware_family, software_version, enabled, last_call, scenario_number) VALUES
    ('T0101001', 'MID000101', 'Desk2600', 'Desktop', '12.4.0', 1, '2026-06-23 12:35:56', NULL),
    ('T0101002', 'MID000101', 'Desk2600', 'Desktop', '12.4.0', 1, '2026-06-20 09:12:03', '5'),
    ('T0102001', 'MID000102', 'Move5000', 'Mobile',  '10.1.2', 1, '2026-06-25 18:02:44', NULL),
    -- last_call = NULL επίτηδες: terminal που δεν έχει καλέσει ΠΟΤΕ,
    -- ώστε να τεσταριστεί το αντίστοιχο bucket στο Feature D4.
    ('T0102002', 'MID000102', 'Move5000', 'Mobile',  '10.1.2', 1, NULL, NULL),
    ('T0103001', 'MID000103', 'Lane3000', 'Lane',    '9.9.9',  0, '2026-01-10 08:00:00', '0');

-- template_id ΔΕΝ δίνεται εδώ — είναι AUTO_INCREMENT, οπότε το πρώτο
-- INSERT παίρνει template_id=1, το δεύτερο template_id=2 (ταιριάζει με το
-- παράδειγμα της εκφώνησης στο B3: {"template_id": 1, ...}).
INSERT INTO templates (hardware_model, hardware_family, software_version, description) VALUES
    ('Desk2600', 'Desktop', '12.4.0', 'Standard countertop terminal'),
    ('Move5000', 'Mobile',  '10.1.2', 'Portable wireless terminal');

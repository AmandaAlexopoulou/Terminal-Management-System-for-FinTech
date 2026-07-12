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

INSERT INTO terminals (tid, mid, hardware_model, software_version, enabled, last_call, scenario_number) VALUES
    ('T0101001', 'MID000101', 'Desk2600', '12.4.0', 1, '2026-06-23 12:35:56', NULL),
    ('T0101002', 'MID000101', 'Desk2600', '12.4.0', 1, '2026-06-20 09:12:03', '5'),
    ('T0102001', 'MID000102', 'Move5000', '10.1.2', 1, '2026-06-25 18:02:44', NULL),
    ('T0103001', 'MID000103', 'Lane3000', '9.9.9',  0, '2026-01-10 08:00:00', '0');

INSERT INTO templates (template_id, hardware_model, software_version, description) VALUES
    ('TPL-DESK26', 'Desk2600', '12.4.0', 'Standard countertop terminal'),
    ('TPL-MOVE50', 'Move5000', '10.1.2', 'Portable wireless terminal');

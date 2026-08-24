-- 002_job_number_range.sql — reserve a block of job numbers for this platform.
--
-- The sequence was seeded above the legacy high-water mark (JN-6889), which
-- sits INSIDE the series iTrade is still issuing from. Two systems drawing
-- from one series will eventually hand out the same number, and the
-- collision would not surface until both reached Xero with invoices against
-- each (ADR-28, ADR-29).
--
-- The fix is a block this platform owns exclusively, agreed with whoever
-- runs iTrade. Until that agreement exists the range is NULL and allocation
-- REFUSES — which is the correct default: no range, no issuing.

ALTER TABLE job_number_sequence ADD COLUMN range_start INTEGER;
ALTER TABLE job_number_sequence ADD COLUMN range_end   INTEGER;
ALTER TABLE job_number_sequence ADD COLUMN range_note  TEXT;

-- Deliberately left NULL. Setting it is an explicit act:
--   python3 tools/job_number_range.py --db /data/ops.db --from 9000 --to 9999 \
--       --note "agreed with <name>, <date>"
--
-- next_value stays where it is; setting a range moves it to range_start.

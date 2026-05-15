"""
Repair engine — diagnose-and-route automatic repair for QA-quarantined motions.

A quarantined motion has data_status='disputed' with a data_status_reason
pointing to a qa_* pattern_id. The engine dispatches each motion to the
handler whose can_handle() returns True. The handler modifies the data
(re-resolves names, re-extracts a field, demotes the motion, etc.) and
returns a RepairResult.

After each repair attempt, run_patterns is re-run. If the underlying QA
finding clears, the motion auto-flips back to 'clean'. If it doesn't, the
motion stays quarantined and the attempt is recorded in motion.meta.
"""

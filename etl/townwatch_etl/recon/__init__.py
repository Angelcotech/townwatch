"""
Recon harness — mechanical enforcement of the recon verification discipline.

Recon itself is agentic research (search engines, page reading, judgment);
this package is the part that must NOT be left to judgment: a validator that
refuses to record an ABSENCE CLAIM ("agendas not published", "no custodian
email", "no public-comment channel") unless the entry carries attestations
proving both required verification methods ran:

  * structure sweep — the CMS section's sibling/year subpages were enumerated
    (one empty subpage is never the section), and
  * search-engine pass — independent queries were run and recorded (two false
    CCSD claims were sitting on first-page Google results, 2026-06-11).

plus a sibling-record control note, and adversarial votes before an absence
claim may carry high confidence. See research/ga_recon/METHODOLOGY.md §3 and
research/state_pipeline/STATE_PLAYBOOK.md "Verification discipline" — this
package is those documents' enforcement layer, run by the recon skill before
any registry write.
"""

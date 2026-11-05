5 November 2026 — translation review, simple-v2

Saved responses were replayed locally before fresh inference. Of six rejected
Qwen 27B reasoning-off responses, four now validate. D1_en_ru, PT1 and F_EL1
supplied unsolicited corrections alongside valid reviews. Those corrections
were discarded. VI2 repeated a matching target quote. Its material-error
judgments now validate; the proposed correction still needs a separate check.
D1_ru_en has an unmatched quotation. F_RU1 has malformed JSON. Both remain
withheld. Six of seven saved Gemma-off failures validate after accepting an
omitted correction. F_VI1 still lacks explanations. The 13 replays took 0.007 s,
with no model calls. This replay measures response usability only. The prior
Qwen miss on L2_en_ru (covenant broadened to contract) remains relevant.

The fresh run used six previously unreviewed excerpts across four languages.
These were selected before inference from four publisher/company pages and two
archived Greek headlines. Some publishers and companies also appear in the
frozen corpus. This is a small development sample with no independent labels.
All six translations came from the installed Argos translator. Local Qwen 27B
used 32,768 context, temperature 0, seed 42 and reasoning off. Five of the six
first-pass reviews proposed corrections, each checked in a separate request
to the same model. All eleven calls completed. All results were shadow-only.

Case         Argos and reviewer observations                      Shadow routing
PT-loss      Loss, currencies and year-on-year comparison held.   Argos
TR-capacity  Commissioning was lost and Çiğli became Cheli.        Withheld
VI-return    After-tax ROAE became a tax-based profit rate.        Withheld
VI-quarter   Phú Mỹ became Fuji; operations detail was omitted.   Withheld
EL-7         Publisher placement muddled the CEO announcement.    Correction
EL-9         Share capital increase became the acronym PMC.       Correction

Qwen alleged a Greek surname spelling change in EL-7 while comparing identical
Latin spellings. The quotations matched, but the explanation does not establish
that error. The selected Greek corrections are experimental. For TR-capacity
and VI-quarter, the reviewer accepted the corrections but numeric rules blocked
spelled-out ordinals and Roman-versus-Arabic numerals. For VI-return the
correction still omitted the explicit after-tax qualifier, and the scale rule
also treated the Vietnamese word for rate in this phrase as a magnitude cue.

Elapsed time: Argos 1.13 s including initialization; Qwen model load 8.47 s;
Qwen evaluation 71.06 s across six reviews and five correction checks. Qwen
reported zero reasoning tokens. Score eligibility was false for every case.

Local audit files in .local/translation-benchmark/dev-20261105 include
saved-replay.json, fresh-cases.json, prepare.py and qwen-off/results.json.
The Greek archive locators in saved-sources.json are headline indices 7 and 9.

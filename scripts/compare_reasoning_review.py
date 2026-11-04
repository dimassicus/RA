"""Report reasoning runs and matched transport controls without publishing traces."""
from collections import Counter
import html
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.evaluate_translation_review import summarize,load_cases
from scripts.translation_review.fresh_cases import CASES

WORK=ROOT/'.local/translation-benchmark/reasoning-review'
DEST=ROOT/'docs/translation-evaluation/2026-11-04'


def metrics(rows):
    summary=summarize(rows)
    summary['seconds']=round(sum(r['seconds'] for r in rows),2)
    summary['correction_checks']=sum('correction' in r for r in rows)
    summary['reasoning_tokens_validated_calls']=0
    for r in rows:
        for m in [r.get('metadata',{}),r.get('correction',{}).get('metadata',{})]:
            summary['reasoning_tokens_validated_calls']+=m.get('usage',{}).get('completion_tokens_details',{}).get('reasoning_tokens',0)
    return summary


def failure_counts(rows):
    counts=Counter()
    for row in rows:
        if row['review_status']!='failed':
            continue
        message=row.get('error','')
        if any(s in message for s in ('Expecting ', 'Extra data:', 'Unterminated ', 'Invalid control character')):
            category='Malformed final JSON'
        elif 'exact quote' in message:
            category='Evidence quote does not match input'
        elif 'Reasoning' in message or 'reasoning' in message:
            category='Reasoning mode not verified'
        elif 'Incomplete generation' in message:
            category='Generation did not finish normally'
        else:
            category='Other response validation or request failure'
        counts[category]+=1
    return dict(counts)


def public_case(c):
    return {k:c.get(k) for k in ('id','kind','source_sha256','reference','review_status','status','selected',
            'score_eligible','diagnostics','seconds','error')} | {
        'verdicts':{n:a['verdict'] for n,a in c['assessments'].items()}}


def main():
    runs={};tables={};original={};identities={}
    for model in ('qwen','gemma'):
        raw={s:json.loads((WORK/f'{model}-{s}'/'results.json').read_text()) for s in ('on','off-old','off-fresh')}
        identities[model]={s:r['identity'] for s,r in raw.items()}
        assert len(raw['on']['results'])==46 and len(raw['off-old']['results'])==34 and len(raw['off-fresh']['results'])==12
        assert len({r['identity']['prompt_sha256'] for r in raw.values()})==1
        assert len({r['identity']['implementation_sha256'] for r in raw.values()})==1
        runs[model]={'on':raw['on']['results'],'off':raw['off-old']['results']+raw['off-fresh']['results']}
        original[model]=json.loads((ROOT/f'.local/translation-benchmark/lmstudio-review/{model}/results.json').read_text())['results']
        for on,off in zip(runs[model]['on'],runs[model]['off']):
            assert on['id']==off['id'] and on['source_sha256']==off['source_sha256']
            assert not on['score_eligible'] and not off['score_eligible']
        for old,on in zip(original[model],runs[model]['on'][:34]):
            assert old['id']==on['id'] and old['source_sha256']==on['source_sha256']
        tables[model]={'old_previous':metrics(original[model]),
                       'old_off':metrics(runs[model]['off'][:34]),'old_on':metrics(runs[model]['on'][:34]),
                       'fresh_off':metrics(runs[model]['off'][34:]),'fresh_on':metrics(runs[model]['on'][34:])}
    assert len({json.loads((WORK/f'{m}-on'/'results.json').read_text())['identity']['source_corpus_sha256'] for m in runs})==1
    # The narrative below describes this dated snapshot; refuse to attach it
    # silently to changed results from a later experiment.
    indexed={m:{mode:{r['id']:r for r in rows} for mode,rows in modes.items()} for m,modes in runs.items()}
    assert indexed['gemma']['on']['PT1']['selected']=='correction'
    assert all(indexed['qwen'][mode]['PT1']['selected']=='madlad' for mode in ('off','on'))
    assert indexed['gemma']['on']['PT2']['selected']=='baseline'
    assert indexed['qwen']['off']['PT2']['selected']=='correction'
    assert {r['id'] for r in runs['gemma']['on'] if r['review_status']=='failed'}=={'L3_ru_en','F_VI1'}
    joint=[]
    allcases=load_cases()+CASES
    for case,q,g in zip(allcases,runs['qwen']['on'],runs['gemma']['on']):
        assert case['id']==q['id']==g['id']
        selected=None
        if all(r['review_status']=='completed' for r in (q,g)):
            for engine in case.get('candidate_order',('baseline','madlad')):
                if all(r['assessments'].get(engine,{}).get('verdict')=='faithful' and
                       not any(f['code']!='literal_html_entity' for f in r['diagnostics'].get(engine,[])) for r in (q,g)):
                    selected=engine;break
        joint.append({'id':case['id'],'selected':selected,'known_bad':bool(selected and case['reference'].get(selected)=='material')})
    DEST.mkdir(parents=True,exist_ok=True)
    failures={m:{mode:failure_counts(rows) for mode,rows in modes.items()} for m,modes in runs.items()}
    output={'counts':tables,'joint_on':joint,'failures':failures,'identities':identities,
            'cases':{m:{mode:[public_case(c) for c in rows] for mode,rows in modes.items()} for m,modes in runs.items()}}
    (DEST/'reasoning-review-summary.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
    e=html.escape
    parts=['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           '<title>RA reasoning-enabled reviewer comparison</title>',
           '<style>body{font:16px/1.6 system-ui;color:#203040;max-width:1350px;margin:30px auto;padding:20px}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{border:1px solid #cad3dc;text-align:left;padding:10px;vertical-align:top}.scroll{overflow:auto}.notice{background:#fff3d6;padding:16px}section{border-top:1px solid #ccd;padding-top:18px;margin-top:35px}pre{white-space:pre-wrap}a{color:#175b92}</style>',
           '<h1>Reasoning enabled: Qwen3.8 27B and Gemma 4 31B</h1>',
           '<p><a href="lmstudio-reviewer-comparison.html">Previous reviewer comparison</a> · <a href="translation-fidelity-comparison.html">Original fictional translations</a> · <a href="watchlist-multilingual-comparison.html">Watchlist article excerpts</a> · <a href="reasoning-review-summary.json">Counts and reproducibility metadata</a></p>',
           '<p>4 November 2026. All runs are isolated from the app. Existing translator and scoring are unchanged. No analyst foreign-language approval step is required: unresolved results are withheld in this experiment.</p>',
           '<p><b>Recommendation:</b> prefer Gemma with reasoning for the next stage of source-based quality review, while keeping the existing translator. Qwen with reasoning is unreliable in this installed configuration. Qwen without reasoning is a credible faster alternative and has better labelled-test detection and response reliability; Gemma’s advantage is the deeper real-source review demonstrated by the Portuguese ambiguity below. This is a choice for further evaluation, not approval for unattended translation corrections.</p>',
           '<p class="notice">Original benchmark: 34 cases, 81 translations, ten prior provisional material-error labels. Fresh benchmark: 12 fictional cases with 24 manually authored candidate translations, nine deliberately wrong and 15 correct. These are assistant-authored test labels, not independent native-speaker ground truth. The fresh set includes targeted regressions, four clean pairs and one shared-error pair; it is not a representative news sample.</p>',
           '<p>Reasoning and matched-off runs use the same unconstrained JSON interface, schema instructions, temperature 0, seed 42, 32768 context and 8192 output-token cap. On requests ask for medium reasoning and require observed reasoning tokens plus a separate final answer. Strict final-answer validation is unchanged. The earlier runs used JSON grammar constraints, 16384 context and a 4096-token cap: compare the matched-off column to isolate the reasoning change. No model output was retried to obtain a preferred verdict.</p>']
    measures=[('Cases tested','cases'),('Known errors in test set','known_material'),
              ('Known errors wrongly approved','material_missed'),('Known errors flagged material','material_detected'),
              ('Known errors marked uncertain','material_marked_uncertain'),('Known errors without a valid assessment','material_unassessed'),
              ('Previously correct/nonmaterial candidates rejected','nonmaterial_rejected_by_reviewer'),
              ('Case responses/checks failed','failed'),('Cases withheld','translation_uncertain'),
              ('Known-bad candidates selected','known_bad_selected'),('Runtime seconds, including correction checks','seconds')]
    for group,title,cols in [('old','Original cases: before, matched off, reasoning on',['previous','off','on']),
                              ('fresh','Fresh cases: matched off versus reasoning on',['off','on'])]:
        headers=[(m,f'{group}_{c}',f'{m.title()} — {c}') for m in runs for c in cols]
        parts.append('<h2>'+title+'</h2><div class="scroll"><table><tr><th>Measure</th>'+''.join('<th>'+e(t)+'</th>' for _,_,t in headers)+'</tr>')
        for label,key in measures:
            parts.append('<tr><th>'+e(label)+'</th>'+''.join('<td>'+str(tables[m][k].get(key,0))+'</td>' for m,k,_ in headers)+'</tr>')
        parts.append('</table></div>')
    parts.append('<p>Invalid responses are withheld, not counted as correct detection. The nonmaterial-rejection count covers valid candidate assessments only; failed responses can also withhold good translations, so read it alongside the failure and withheld counts. Corrections receive a separate check by the same model, so repeated approval remains correlated. A selected correction is not automatically established as correct. Original publisher examples lie outside the ten-error metric; shared linguistic errors can persist even when the labelled-error count improves.</p>')
    parts.append('<p>Original-case routing prefers the existing translator, then MADLAD, then a separately checked correction. NLLB is audited but is not eligible for selection in this experiment. Fresh cases prefer candidate_1 when both pass. A faithful verdict may still be blocked by deterministic checks, including overly strict number/date formatting checks. No selection is enabled for live scoring.</p>')
    parts.append('<h2>Important examples behind the recommendation</h2><ul>'
                 '<li>PT1, real Portuguese restructuring news: Gemma with reasoning caught the shift from possibility to eventual certainty, rejected both candidates and proposed a separately checked correction. Qwen still selected the MADLAD candidate with that mistake, both with and without reasoning.</li>'
                 '<li>PT2, real Portuguese financial figures: Gemma with reasoning retained the existing translation. Qwen without reasoning proposed a rewrite even though the main figures and meaning were already preserved.</li>'
                 '<li>VI2, real Vietnamese projections: both reasoning models corrected invented currencies back to VND/dong and preserved projected profits and no borrowing plans. These corrections were checked by the same model, not independently verified.</li>'
                 '<li>Gemma’s two reasoning failures were evidence-quote mismatches: an HTML apostrophe was decoded in L3_ru_en, and separate source fragments were joined with an ellipsis in F_VI1. These are validation failures, not proof of a missed semantic error; they remain withheld and unassessed in the metric.</li>'
                 '<li>Qwen without reasoning and Gemma with reasoning still approved the disputed NLLB covenant wording. The previous labels remain unchanged and provisional.</li>'
                 '</ul><p>The next evaluation should simplify response formatting and improve number/date and evidence-span handling while preserving automatic withholding on unresolved cases. It should then use new examples and an independent reviewer before any live correction or scoring integration. Running both models and accepting their agreement is not sufficient evidence of correctness.</p>')
    parts.append('<h2>Response reliability across all 46 cases</h2><table><tr><th>Reviewer / mode</th><th>Failure categories</th></tr>')
    for model,modes in failures.items():
        for mode,counts in modes.items():
            parts.append('<tr><td>'+e(model+' / '+mode)+'</td><td>'+e('; '.join(f'{k}: {v}' for k,v in counts.items()) or 'None')+'</td></tr>')
    parts.append('</table><p>No malformed response was repaired into a pass. Counts include failures during a proposed correction check, which can occur after a valid initial candidate assessment.</p>')
    parts.append('<h2>Both reasoning reviewers must agree</h2><p>'+str(sum(bool(c['selected']) for c in joint))+' of 46 cases retain an existing candidate; '+str(sum(not c['selected'] for c in joint))+' are withheld. '+str(sum(c['known_bad'] for c in joint))+' previously labelled bad candidates would be selected. Newly proposed corrections are excluded from this joint simulation; both models have not independently reviewed those exact corrections.</p>')
    parts.append('<h2>Original-case routing</h2><div class="scroll"><table><tr><th>Case</th><th>Qwen off</th><th>Qwen on</th><th>Gemma off</th><th>Gemma on</th></tr>')
    for i,case in enumerate(allcases[:34]):
        cells=[]
        for m in runs:
            for mode in ('off','on'):
                row=runs[m][mode][i]
                verdicts='; '.join(n+': '+a['verdict'] for n,a in row['assessments'].items()) or row.get('error','No valid assessment')
                cells.append('<td><b>'+e(row['selected'] or 'withheld')+'</b><br>'+e(verdicts)+'</td>')
        parts.append('<tr><th>'+e(case['id'])+'</th>'+''.join(cells)+'</tr>')
    parts.append('</table></div><h2>Fresh cases: source, candidate texts and reviewer decisions</h2>')
    for i,case in enumerate(CASES,34):
        parts.append('<section><h3>'+e(case['id'])+' · '+e(case['source_lang']+' → '+case['target_lang'])+'</h3><p>'+e(case['focus'])+'</p><p><b>Source</b></p><pre>'+e(case['source'])+'</pre>')
        parts.append('<div class="scroll"><table><tr>'+''.join('<th>'+e(name+' — expected '+case['reference'][name])+'</th>' for name in case['candidates'])+'</tr><tr>'+''.join('<td><pre>'+e(text)+'</pre></td>' for text in case['candidates'].values())+'</tr></table></div>')
        parts.append('<table><tr><th>Reviewer</th><th>Decision</th><th>Candidate judgments</th></tr>')
        for model in runs:
            for mode in ('off','on'):
                row=runs[model][mode][i]
                verdicts='; '.join(n+': '+a['verdict'] for n,a in row['assessments'].items()) or row.get('error','No valid assessment')
                parts.append('<tr><td>'+e(model+' / '+mode)+'</td><td>'+e(row['selected'] or 'withheld')+'</td><td>'+e(verdicts)+'</td></tr>')
        parts.append('</table><details><summary>Reasoning-enabled reviewer observations and proposed corrections</summary>')
        for model in runs:
            row=runs[model]['on'][i]
            for name,a in row['assessments'].items():
                for issue in a['issues']:
                    parts.append('<p><b>'+e(model+' / '+name)+'</b>: '+e(issue['explanation'])+'</p>')
            if row.get('correction'):
                parts.append('<p><b>'+e(model)+' proposed correction (unverified)</b></p><pre>'+e(row['correction']['text'])+'</pre>')
        parts.append('</details></section>')
    parts.append('<p>Raw reasoning traces are not reproduced. Full originals of publisher articles are not reproduced. Inputs, model responses and hashes remain in the ignored local experiment directory; these summaries include only case identifiers and judgments for publisher examples.</p></html>')
    (DEST/'reasoning-review-comparison.html').write_text('\n'.join(parts))
    print(json.dumps(tables,indent=2))


if __name__=='__main__':main()

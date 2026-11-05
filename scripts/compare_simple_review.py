"""Compare frozen simplified-protocol runs to the previous matched benchmark."""
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.compare_reasoning_review import metrics,public_case
from scripts.evaluate_translation_review import load_cases
from scripts.translation_review.fresh_cases import CASES
from scripts.translation_review.simple import SYSTEM
from scripts.translation_review.evidence import canonical,resolve_spans
from scripts.translation_review.reasoning import parse_final
from scripts.translation_review.reviewer import validate_response

WORK=ROOT/'.local/translation-benchmark/simple-review'
PREVIOUS=ROOT/'.local/translation-benchmark/reasoning-review'
DEST=ROOT/'docs/translation-evaluation/2026-11-04'


def failures(rows):
    counts=Counter()
    for row in rows:
        if row['review_status']!='failed':continue
        message=row.get('error','')
        if any(s in message for s in ('Expecting ','Extra data:','Unterminated ','Invalid control')):
            kind='Malformed final JSON'
        elif 'Duplicate evidence span' in message:kind='Repeated evidence fragment'
        elif 'evidence' in message.lower() or 'quote' in message.lower():kind='Evidence validation'
        elif 'Correction offered without' in message:kind='Unrequested correction alongside approved candidate'
        elif any(s in message for s in ('response shape','issue fields','review fields','candidate coverage')):
            kind='Missing or unexpected response fields'
        elif 'Incomplete generation' in message:kind='Incomplete generation'
        else:kind='Other response validation or request failure'
        counts[kind]+=1
    return dict(counts)


def decision(row):
    judgments='; '.join(f'{name}: {a["verdict"]}' for name,a in row['assessments'].items())
    return (row['selected'] or 'withheld')+' — '+(judgments or row.get('error','No valid assessment'))


def audit_rows(cases,rows,mode):
    counts=Counter()
    for case,row in zip(cases,rows):
        candidates={a:case['candidates'][n] for a,n in row['aliases'].items()}
        calls=[(row.get('metadata'),candidates)]
        if row.get('correction'):
            calls.append((row['correction']['metadata'],{'X':row['correction']['text']}))
        for metadata,texts in calls:
            if metadata is None:continue
            counts['validated_calls']+=1
            tokens=metadata['usage'].get('completion_tokens_details',{}).get('reasoning_tokens',0)
            chars=metadata['reasoning_chars']
            assert metadata['reasoning_enabled']==(mode=='on')
            assert (tokens>0 and chars>0) if mode=='on' else (tokens==0 and chars==0)
            for label,issues in metadata['evidence_spans'].items():
                for issue in issues:
                    for side,text in [('source',case['source']),('translation',texts[label])]:
                        for span in issue[side]:
                            assert text[span['start']:span['end']]==span['text']
                            assert canonical(span['text'])[0].strip()==canonical(span['quote'])[0].strip()
                            counts['original_spans_verified']+=1
                            counts['normalized_spans']+=bool(span['normalized_match'])
        invalid=row.get('invalid_response')
        if invalid:
            counts['rejected_calls']+=1
            tokens=invalid.get('usage',{}).get('completion_tokens_details',{}).get('reasoning_tokens',0)
            chars=len(invalid['choices'][0]['message'].get('reasoning_content') or '')
            observed=(tokens>0 and chars>0) if mode=='on' else (tokens==0 and chars==0)
            counts['rejected_calls_with_expected_reasoning_mode']+=observed
    return dict(counts)


def evidence_only_recheck(cases):
    """Diagnostic only: never replace an earlier recorded verdict or result."""
    indexed={c['id']:c for c in cases}; output=[]
    for model in ('qwen','gemma'):
        for mode in ('on','off'):
            rows=[]
            for suffix in (['on'] if mode=='on' else ['off-old','off-fresh']):
                rows+=json.loads((PREVIOUS/f'{model}-{suffix}/results.json').read_text())['results']
            for row in rows:
                if row['review_status']!='failed' or 'quote' not in row.get('error',''):continue
                case=indexed[row['id']]; recovered=False
                try:
                    data,_=parse_final(row['invalid_response']['choices'][0]['message']['content'])
                    candidates={a:case['candidates'][n] for a,n in row['aliases'].items()}
                    for assessment in data['assessments']:
                        for issue in assessment['issues']:
                            src=resolve_spans(case['source'],[issue['source_quote']])
                            dst=resolve_spans(candidates[assessment['candidate']],
                                              [issue['translation_quote']]) if issue['translation_quote'] else []
                            issue['source_quote']=src[0]['text']
                            issue['translation_quote']=dst[0]['text'] if dst else ''
                    validate_response(data,case['source'],candidates); recovered=True
                except (ValueError,KeyError,TypeError):pass
                output.append({'model':model,'mode':mode,'case':row['id'],
                               'normalization_alone_validates_response':recovered})
    return output


def main():
    cases=load_cases()+CASES
    assert len(cases)==46 and sum(len(c['candidates']) for c in cases)==105
    assert json.loads((WORK/'inputs-frozen.json').read_text())==cases
    implementation_files=[ROOT/'scripts/evaluate_simple_review.py',ROOT/'scripts/evaluate_translation_review.py',
        *[ROOT/'scripts/translation_review'/f for f in ('simple.py','evidence.py','reasoning.py','fresh_cases.py','lmstudio.py','reviewer.py','checks.py')]]
    implementation_hash=hashlib.sha256(b''.join(f.read_bytes() for f in implementation_files)).hexdigest()
    corpus_hash=hashlib.sha256(json.dumps(cases,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    runs={};identities={};counts={};errors={};audits={}
    for model in ('qwen','gemma'):
        runs[model]={};identities[model]={};counts[model]={};errors[model]={};audits[model]={}
        for mode in ('off','on'):
            new=json.loads((WORK/f'{model}-{mode}/results.json').read_text())
            assert len(new['results'])==46
            identity=new['identity'];identities[model][mode]=identity
            assert identity['mode']==mode and identity['suite']=='all'
            assert identity['implementation_sha256']==implementation_hash
            assert json.loads((WORK/f'{model}-{mode}/inputs.json').read_text())==cases
            assert identity['source_corpus_sha256']==corpus_hash
            assert identity['prompt_sha256']==hashlib.sha256(SYSTEM.encode()).hexdigest()
            assert identity['protocol']=='simple-v1'
            assert identity['temperature']==0 and identity['seed']==42 and identity['max_output_tokens']==8192
            audits[model][mode]=audit_rows(cases,new['results'],mode)
            old=[]
            for suffix in (['on'] if mode=='on' else ['off-old','off-fresh']):
                prior=json.loads((PREVIOUS/f'{model}-{suffix}/results.json').read_text())
                assert {k:v for k,v in identity['model_identity'].items() if k!='id'}=={k:v for k,v in prior['identity']['model_identity'].items() if k!='id'}
                for key in ('temperature','seed','max_output_tokens','reasoning_effort'):
                    assert identity[key]==prior['identity'][key]
                old+=prior['results']
            assert len(old)==46
            for version,rows in [('before',old),('after',new['results'])]:
                name=f'{mode}_{version}';runs[model][name]=rows
                counts[model][name]={group:metrics(group_rows) for group,group_rows in
                                    [('all',rows),('original',rows[:34]),('previously_fresh',rows[34:])]}
                errors[model][name]=failures(rows)
                for row,case in zip(rows,cases):
                    assert row['id']==case['id'] and not row['score_eligible']
                    assert row['source_sha256']==hashlib.sha256(case['source'].encode()).hexdigest()
    assert len({i['implementation_sha256'] for m in identities.values() for i in m.values()})==1
    # This narrative describes the dated snapshot, not an arbitrary future run.
    indexed={m:{v:{r['id']:r for r in rows} for v,rows in modes.items()} for m,modes in runs.items()}
    for model in runs:
        assert counts[model]['on_after']['all']['completed']==46
        assert counts[model]['on_after']['all']['material_detected']==18
        assert indexed[model]['on_after']['L2_en_ru']['assessments']['nllb']['verdict']=='faithful'
        assert indexed[model]['on_after']['PT1']['selected']=='madlad'
        assert indexed[model]['on_after']['VI2']['selected']=='correction'
    assert indexed['gemma']['on_before']['PT1']['selected']=='correction'
    assert indexed['gemma']['on_after']['EL2']['selected']=='madlad'
    assert indexed['qwen']['on_after']['EL2']['selected']=='correction'
    assert counts['gemma']['on_after']['all']['nonmaterial_rejected_by_reviewer']==1
    assert counts['qwen']['on_after']['all']['nonmaterial_rejected_by_reviewer']==5
    ablation=evidence_only_recheck(cases)
    (WORK/'evidence-ablation.json').write_text(json.dumps(ablation,indent=2))
    output={'counts':counts,'failures':errors,'identities':identities,'audits':audits,'evidence_only_recheck':ablation,
            'cases':{m:{v:[public_case(r) for r in rows] for v,rows in modes.items()} for m,modes in runs.items()}}
    DEST.mkdir(parents=True,exist_ok=True)
    (DEST/'simple-review-summary.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
    e=html.escape
    parts=['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           '<title>Simplified translation review — Qwen and Gemma retest</title>',
           '<style>body{font:16px/1.55 system-ui;color:#203040;max-width:1500px;margin:30px auto;padding:20px}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{border:1px solid #cad3dc;padding:10px;text-align:left;vertical-align:top}.scroll{overflow:auto}.notice{background:#fff3d6;padding:16px}pre{white-space:pre-wrap}section{border-top:1px solid #ccd;margin-top:30px;padding-top:15px}a{color:#175b92}</style>',
           '<h1>Simplified translation reviewer: Qwen and Gemma retest</h1>',
           '<p>Evaluation series started 4 November 2026; simplified retest continued 5 November. App, translator, database and live scoring unchanged. Unresolved cases are automatically withheld in the experiment; no analyst foreign-language approval step is introduced.</p>',
           '<p><a href="reasoning-review-comparison.html">Previous reasoning comparison</a> · <a href="simple-review-summary.json">Counts and run identities</a></p>',
           '<h2>Result and recommendation</h2><p><b>Prefer Gemma with reasoning for the next isolated reviewer trial; keep the existing translator and app unchanged.</b> Both reasoning-on configurations returned valid results for all 46 cases and flagged 18 of 19 provisionally labelled material errors. Gemma rejected one of 70 previously nonmaterial candidates versus five for Qwen, and completed in 38.39 minutes versus 44.30. These are small-sample operational results, not established production accuracy.</p>',
           '<p>Qwen reasoning-on failures fell from 20 to zero; Gemma reasoning-on failures fell from two to zero. This supports the diagnosis that the old response protocol was a major obstacle for Qwen. The change is not a universal improvement: Qwen off regressed from zero to six failed cases, partly because of new protocol restrictions. Gemma off improved from 28 failures to seven, but still approved three labelled errors. Neither new reasoning-off configuration is the preferred reviewer.</p>',
           '<p><b>Remaining semantic limitations:</b> both reasoning models still approve the disputed NLLB covenant wording. Gemma now also accepts the real Portuguese candidate whose possibility/timing nuance it flagged in the previous run. That earlier real-source advantage is no longer stable under the simpler prompt, even though both models catch the more explicit error in the short Portuguese regression sentence. No labelled-bad candidate is selected by this experiment, but NLLB is excluded from selection and publisher examples lie outside the nineteen-error metric; this is not proof of safe automatic approval.</p>',
           '<p>Gemma makes fewer questionable rewrites in these examples: it retains MADLAD for the Greek borrowing/lending article, whereas Qwen additionally objects to singular versus plural dividends and rewrites it. Gemma also accepts the clean Portuguese and Turkish regression paraphrases that Qwen rejects. Both propose separately checked corrections for invented Vietnamese currencies; the second check uses the same model and is not independent verification.</p>',
           '<p><b>Next:</b> retain the compact verdict-and-evidence design, separate optional correction output and harmless duplicate evidence from substantive judgment failures, and evaluate fresh articles against independently established meanings before enabling automatic correction. Preserve automatic withholding for unresolved meaning. Date/number formatting checks still cause some unnecessary withholding and need their own controlled follow-up.</p>',
           '<p class="notice">Each configuration uses the same 46 cases and 105 candidate translations. Nineteen candidates have provisional material-error labels. The twelve formerly fresh fictional cases are now regression examples, not a new held-out test. Labels are assistant judgments; this is not an estimate of production accuracy.</p>',
           '<h2>What changed</h2><p>The response now contains one candidate verdict and a list of material issues. The nested seven-category coverage grid and wording-only issue reports are removed. The prompt still asks the model to check all seven dimensions. Uncertain results remain ineligible; corrections are permitted only when every candidate is rejected for a material error and still receive a separate same-model check.</p>',
           '<p>Evidence uses arrays of separate contiguous quotations. Validation preserves offsets into original text, allowing one-layer HTML entity decoding, NFC accent composition, typographic quotation marks and whitespace changes. It does not change words, case, numbers, currencies or negation; invent ellipses; or accept word fragments inside a different word. Invalid JSON and unsupported verdicts remain failures. Number/date routing diagnostics are unchanged to avoid confounding this retest.</p>',
           '<p>Settings remain temperature 0, seed 42, 32768 context, 8192 output tokens, medium reasoning on versus none off, and unconstrained final JSON. Reasoning-on success requires observed reasoning tokens and a separate final answer. No retries or per-case prompt tuning. This compares a changed prompt/protocol and evidence handling together, not a pure evidence-normalization ablation.</p>',
           '<p>Installed configurations: Qwen 27B uses MLX 5-bit weights; Gemma 31B uses GGUF Q4_0 QAT weights. Timings describe these local configurations, include correction checks and are not a hardware-normalized comparison of model families. Each configuration was run once.</p>']
    for group,title in [('all','All 46 cases'),('previously_fresh','Twelve previously fresh cases')]:
        parts.append('<h2>'+title+'</h2><div class="scroll"><table><tr><th>Model / mode / version</th><th>Errors flagged</th><th>Errors approved</th><th>Errors uncertain</th><th>Errors unassessed</th><th>Valid cases</th><th>Previously nonmaterial candidates rejected</th><th>Cases withheld</th><th>Labelled-bad candidates selected</th><th>Minutes</th></tr>')
        for model in runs:
            for name in ('off_before','off_after','on_before','on_after'):
                c=counts[model][name][group]
                values=[model+' / '+name.replace('_',' / '),f'{c.get("material_detected",0)}/{c["known_material"]}',
                        c.get('material_missed',0),c.get('material_marked_uncertain',0),c.get('material_unassessed',0),
                        f'{c.get("completed",0)}/{c["cases"]}',c.get('nonmaterial_rejected_by_reviewer',0),
                        c.get('translation_uncertain',0),c.get('known_bad_selected',0),round(c['seconds']/60,2)]
                parts.append('<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in values)+'</tr>')
        parts.append('</table></div>')
    parts.append('<p>Errors without a valid assessment are not counted as detected. A failed correction check can follow valid initial candidate assessments. Nonmaterial-rejection counts cover valid assessments only; failures can also withhold good translations. A selected correction is not independently established as correct. The original publisher excerpts fall outside the nineteen-error metric.</p>')
    parts.append('<h2>Response failures</h2><table><tr><th>Configuration</th><th>Failures</th></tr>')
    for model,modes in errors.items():
        for mode,summary in modes.items():
            parts.append('<tr><td>'+e(model+' / '+mode)+'</td><td>'+e('; '.join(f'{k}: {v}' for k,v in summary.items()) or 'None')+'</td></tr>')
    recovered=[r for r in ablation if r['normalization_alone_validates_response']]
    parts.append('</table><p>Offline rechecking of the '+str(len(ablation))+' old evidence-quotation failures, without another model call or changed verdict, made '+str(len(recovered))+' responses structurally valid using normalization alone: '+e(', '.join(r['model']+' '+r['mode']+' '+r['case'] for r in recovered))+'. The remaining '+str(len(ablation)-len(recovered))+' still fail. This is a response-validation diagnostic, not proof the recovered judgments are semantically correct.</p>')
    parts.append('<h2>New-run failure details</h2><p>These responses remain failed in every metric. A correct judgment elsewhere in a rejected response is not silently recovered into a pass. The new correction constraint and duplicate-fragment rejection are protocol restrictions, not evidence that translation meaning was misunderstood.</p><table><tr><th>Configuration</th><th>Case</th><th>Reason withheld</th></tr>')
    for model in runs:
        for mode in ('off','on'):
            for row in runs[model][mode+'_after']:
                if row['review_status']=='failed':
                    parts.append('<tr><td>'+e(model+' / '+mode)+'</td><td>'+e(row['id'])+'</td><td>'+e(row.get('error',''))+'</td></tr>')
    parts.append('</table><h2>Recorded evidence and mode audit</h2><p>These counts include separate correction checks. Original offsets and normalized quotations were checked again after inference; reasoning modes were checked from returned usage metadata. Quote provenance does not establish that an interpretation is correct.</p><table><tr><th>Configuration</th><th>Validated calls</th><th>Original evidence spans verified</th><th>Spans using normalization</th><th>Rejected calls with expected reasoning mode</th></tr>')
    for model,modes in audits.items():
        for mode,audit in modes.items():
            values=[model+' / '+mode,audit.get('validated_calls',0),audit.get('original_spans_verified',0),
                    audit.get('normalized_spans',0),str(audit.get('rejected_calls_with_expected_reasoning_mode',0))+'/'+str(audit.get('rejected_calls',0))]
            parts.append('<tr>'+''.join('<td>'+e(str(value))+'</td>' for value in values)+'</tr>')
    parts.append('</table>')
    parts.append('<h2>Disagreements with earlier nonmaterial labels</h2><p>These are not automatically model mistakes: earlier labels are provisional. The explanations below make possible over-rejection and label weaknesses inspectable. Only fictional labelled examples appear here.</p><table><tr><th>Configuration</th><th>Case / candidate</th><th>New verdict and explanation</th></tr>')
    for model in runs:
        for mode in ('off','on'):
            for row in runs[model][mode+'_after']:
                for name,assessment in row['assessments'].items():
                    label=row['reference'].get(name)
                    if label is not None and label!='material' and assessment['verdict']!='faithful':
                        reason=assessment['verdict']+': '+'; '.join(i['explanation'] for i in assessment['issues'])
                        parts.append('<tr><td>'+e(model+' / '+mode)+'</td><td>'+e(row['id']+' / '+name)+'</td><td>'+e(reason)+'</td></tr>')
    parts.append('</table>')
    parts.append('<h2>Per-case routing before and after</h2><p>Original routing prefers the existing translator, then MADLAD, then a separately checked correction. NLLB is audited but never selected. The fictional fresh candidates are manually authored. Numeric checks can withhold otherwise approved translations. Every result remains ineligible for live scoring.</p><div class="scroll"><table><tr><th>Case</th>')
    configs=[(m,mode) for m in runs for mode in ('off','on')]
    parts.append(''.join('<th>'+e(m+' '+mode)+'</th>' for m,mode in configs)+'</tr>')
    for i,case in enumerate(cases):
        parts.append('<tr><th>'+e(case['id'])+'</th>')
        for m,mode in configs:
            parts.append('<td><b>Before:</b> '+e(decision(runs[m][mode+'_before'][i]))+'<br><b>After:</b> '+e(decision(runs[m][mode+'_after'][i]))+'</td>')
        parts.append('</tr>')
    parts.append('</table></div><h2>Fictional regression examples: source, translations and new judgments</h2>')
    for i,case in enumerate(CASES,34):
        parts.append('<section><h3>'+e(case['id']+' — '+case['focus'])+'</h3><p><b>Source</b></p><pre>'+e(case['source'])+'</pre><div class="scroll"><table><tr>')
        parts.append(''.join('<th>'+e(n+' / expected '+case['reference'][n])+'</th>' for n in case['candidates'])+'</tr><tr>')
        parts.append(''.join('<td><pre>'+e(t)+'</pre></td>' for t in case['candidates'].values())+'</tr></table></div>')
        for m,mode in configs:
            r=runs[m][mode+'_after'][i]
            parts.append('<details><summary>'+e(m+' / '+mode+' — '+decision(r))+'</summary>')
            for name,a in r['assessments'].items():
                for issue in a['issues']:
                    parts.append('<p><b>'+e(name)+'</b>: '+e(issue['explanation'])+'</p>')
            if r.get('correction'):parts.append('<p>Experimental correction:</p><pre>'+e(r['correction']['text'])+'</pre>')
            parts.append('</details>')
        parts.append('</section>')
    parts.append('<p>No raw reasoning traces or full publisher originals are published here. Local inputs, responses and original evidence offsets remain in the ignored experiment directory. No cloud API calls or model downloads were made.</p></html>')
    (DEST/'simple-review-comparison.html').write_text('\n'.join(parts))
    print(json.dumps({m:{v:c['all'] for v,c in modes.items()} for m,modes in counts.items()},indent=2))


if __name__=='__main__':main()

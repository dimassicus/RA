"""Package comparable local reviewer runs; no model requests or app imports."""
import html
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / '.local/translation-benchmark'
DEST = ROOT / 'docs/translation-evaluation/2026-11-04'
RUNS = {
    'Qwen3 8B · Q4_K_M': WORK/'automated-review',
    'Qwen3.8 27B · 5-bit': WORK/'lmstudio-review/qwen',
    'Gemma 4 31B QAT · Q4_0': WORK/'lmstudio-review/gemma',
}


def joint_routing(left, right):
    """Offline simulation, not independent proof of correctness or deployment."""
    assert left['id'] == right['id'] and left['source_sha256'] == right['source_sha256']
    if any(r['review_status'] != 'completed' for r in (left,right)):
        return None
    for engine in ('baseline','madlad'):
        if all(r['assessments'].get(engine,{}).get('verdict')=='faithful' and
               not any(f['code']!='literal_html_entity' for f in r['diagnostics'].get(engine,[]))
               for r in (left,right)):
            return engine
    return None


def main():
    runs={name:json.loads((path/'results.json').read_text()) for name,path in RUNS.items()}
    assert len({r['identity']['corpus_sha256'] for r in runs.values()})==1
    assert len({r['identity']['prompt_sha256'] for r in runs.values()})==1
    assert all(len(r['results'])==34 for r in runs.values())
    assert all(not c['score_eligible'] for r in runs.values() for c in r['results'])
    names=list(runs);base=runs[names[0]]['results'];q=runs[names[1]]['results'];g=runs[names[2]]['results']
    joint=[]
    for a,b,c in zip(base,q,g):
        assert a['id']==b['id']==c['id'] and a['source_sha256']==b['source_sha256']==c['source_sha256']
        chosen=joint_routing(b,c)
        joint.append({'id':b['id'],'selected':chosen,'score_eligible':False,
                      'known_bad_selected':bool(chosen and b['reference'].get(chosen)=='material')})
    summary={name:{'identity':r['identity'],'counts':r['summary'],
                   'seconds':round(sum(c['seconds'] for c in r['results']),2),
                   'correction_checks':sum('correction' in c for c in r['results'])}
             for name,r in runs.items()}
    combined={'selected':sum(bool(c['selected']) for c in joint),
              'withheld':sum(not c['selected'] for c in joint),
              'known_bad_selected':sum(c['known_bad_selected'] for c in joint)}
    DEST.mkdir(parents=True,exist_ok=True)
    (DEST/'lmstudio-reviewer-summary.json').write_text(json.dumps({'models':summary,'joint':combined,'joint_cases':joint},ensure_ascii=False,indent=2))
    e=html.escape
    parts=['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           '<title>RA local translation reviewers compared</title>',
           '<style>body{font:16px/1.6 system-ui;color:#203040;max-width:1250px;margin:30px auto;padding:20px}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{border:1px solid #cad3dc;text-align:left;padding:10px;vertical-align:top}.scroll{overflow:auto}.notice{background:#fff3d6;padding:16px}a{color:#175b92}</style>',
           '<h1>Local translation reviewers — 4 November 2026</h1>',
           '<p class="notice">Shadow evaluation only. Existing translator, app and scoring remain unchanged. Reported judgments are model outputs, not verified corrections. Unresolved cases are withheld automatically, without analyst foreign-language review.</p>',
           '<p>Same 34 cases and 81 candidate translations; identical source/prompt hashes and randomized anonymous labels. Ten fictional translations had prior provisional material-error labels; 55 had nonmaterial labels. Eight additional publisher excerpts exercise Portuguese, Greek, Turkish and Vietnamese. Those publisher cases are outside the ten-error metric. All reference labels are earlier assistant judgments, not independent expert ground truth.</p>',
           '<div class="scroll"><table><tr><th>Measure</th>'+''.join('<th>'+e(n)+'</th>' for n in names)+'</tr>']
    measures=[('Known material errors detected / 10','material_detected'),
              ('Known material errors incorrectly passed','material_missed'),
              ('Known material errors marked uncertain','material_marked_uncertain'),
              ('Known material errors without valid assessment','material_unassessed'),
              ('Previously nonmaterial outputs rejected / 55','nonmaterial_rejected_by_reviewer'),
              ('Cases with failed response/check / 34','failed'),
              ('Cases withheld / 34','translation_uncertain'),
              ('Known-bad translations selected by routing','known_bad_selected')]
    for label,key in measures:
        parts.append('<tr><th>'+e(label)+'</th>'+''.join('<td>'+str(runs[n]['summary'].get(key,0))+'</td>' for n in names)+'</tr>')
    parts.append('<tr><th>Total inference seconds (excluding load)</th>'+''.join('<td>'+str(summary[n]['seconds'])+'</td>' for n in names)+'</tr></table></div>')
    parts.append('<p>Invalid responses are withheld, not counted as successful error detection. A failed case can contain useful raw observations, but cannot pass the validation rules. False-alarm counts compare against provisional labels and may include legitimate reviewer disagreements. Timing includes any correction checks and differs by runtime and quantization.</p>')
    parts.append('<p>For example, Qwen3.8 described material problems in N1_ru_en, D1_ru_en and L3_ru_en but simultaneously labelled those translations faithful. All three responses were rejected as contradictory. Its zero count of fully validated material-error verdicts must not be interpreted as noticing no problems. It also proposed a useful correction for the Vietnamese currency error; this does not establish reliable legal review.</p>')
    parts.append('<h2>Requiring both larger reviewers to agree</h2><p>Offline simulation: '+str(combined['selected'])+' cases would retain a candidate; '+str(combined['withheld'])+' would be withheld; '+str(combined['known_bad_selected'])+' known-bad fictional translations would be selected. This considers the existing translator and MADLAD only, not new corrections. Agreement is not proof: two reviewers can share an error.</p>')
    parts.append('<p>Concrete shared miss: both larger reviewers approved PT1’s shift from possibility to eventual timing. This publisher-example error is outside the fictional-label metric above. Gemma did flag the Greek borrowing/lending issue and Turkish company/appointment issues. The remaining failures prevent an automatic production approval recommendation.</p>')
    parts.append('<h2>Case-by-case routing</h2><div class="scroll"><table><tr><th>Case</th>'+''.join('<th>'+e(n)+'</th>' for n in names)+'<th>Both larger reviewers</th></tr>')
    for i,c in enumerate(base):
        cells=[]
        for n in names:
            row=runs[n]['results'][i]
            verdicts='; '.join(k+': '+v['verdict'] for k,v in row['assessments'].items()) or row.get('error','No valid review')
            chosen=row['selected'] or 'withhold'
            bad=' — KNOWN BAD' if row['reference'].get(row['selected'])=='material' else ''
            cells.append('<td><strong>'+e(chosen+bad)+'</strong><br>'+e(verdicts)+'</td>')
        parts.append('<tr><th>'+e(c['id'])+'</th>'+''.join(cells)+'<td>'+e(joint[i]['selected'] or 'withhold')+'</td></tr>')
    parts.append('</table></div><h2>Method and full model reports</h2><ul>')
    for name,slug in [(names[1],'qwen'),(names[2],'gemma')]:
        shutil.copyfile(RUNS[name]/'review.html',DEST/f'lmstudio-{slug}-review.html')
        parts.append('<li><a href="lmstudio-'+slug+'-review.html">'+e(name)+' detailed model judgments</a></li>')
    parts.append('</ul><p>Temperature 0, seed 42, 16384-token context and maximum 4096 output tokens. Reasoning explicitly disabled for these LM Studio runs; results do not establish performance with reasoning enabled. Original source text is provided to the reviewer; expected answers, engine names and prior labels are withheld. Proposed corrections receive another call to the same model, which is correlated validation. No cloud API or news-provider requests were used.</p></html>')
    (DEST/'lmstudio-reviewer-comparison.html').write_text('\n'.join(parts))
    print(json.dumps({'models':{n:v['counts'] for n,v in summary.items()},'joint':combined},indent=2))


if __name__=='__main__':
    main()

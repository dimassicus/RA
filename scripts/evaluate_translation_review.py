"""Shadow evaluation only: no app imports, database writes or translator changes."""
import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.translation_review.checks import diagnostics
from scripts.translation_review.reviewer import PROMPT, SCHEMA, ReviewError, model_identity, review
from scripts.translation_review import lmstudio

DEFAULT = ROOT / '.local/translation-benchmark/automated-review'


def load_cases():
    data = json.loads((ROOT / 'docs/translation-evaluation/2026-11-04/translation-fidelity-results.json').read_text())
    cases = {}
    for engine, report in data['reports'].items():
        for row in report['results']:
            if row['status'] != 'ok':
                continue
            case = cases.setdefault(row['id'], {'id': row['id'], 'kind': 'fictional',
                'source_lang': row['source_lang'], 'target_lang': row['target_lang'],
                'source': '\n'.join(row['units']), 'candidates': {}, 'reference': {}})
            assert case['source'] == '\n'.join(o['source'] for o in row['outputs'])
            case['candidates'][engine] = '\n'.join(o['text'] for o in row['outputs'])
            case['reference'][engine] = data['observations']['cases'][row['id']][engine]['level']
    for engine in ('baseline', 'madlad'):
        path = ROOT / f'.local/translation-benchmark/watchlist-languages/{engine}.json'
        if not path.exists():
            continue
        for row in json.loads(path.read_text())['results']:
            assert row['status'] == 'ok'
            case = cases.setdefault(row['id'], {'id': row['id'], 'kind': 'publisher_excerpt',
                'source_lang': row['lang'], 'target_lang': 'en', 'source': '\n'.join(row['units']),
                'url': row['url'], 'candidates': {}, 'reference': {}})
            assert case['source'] == '\n'.join(o['source'] for o in row['outputs'])
            case['candidates'][engine] = '\n'.join(o['text'] for o in row['outputs'])
    return list(cases.values())


def evaluate(case, model, port, rules_only=False, provider='ollama', reviewer=None):
    # Fixed randomized aliases; neither engine names, expected labels nor previous
    # findings are included in the review request.
    names = sorted(case['candidates'])
    random.Random(case['id']).shuffle(names)
    aliases = {chr(65+i): name for i, name in enumerate(names)}
    candidates = {a: case['candidates'][name] for a, name in aliases.items()}
    result = {'id': case['id'], 'kind': case['kind'], 'source_lang': case['source_lang'],
              'target_lang': case['target_lang'], 'source_sha256': hashlib.sha256(case['source'].encode()).hexdigest(),
              'aliases': aliases, 'diagnostics': {n: diagnostics(case['source'], t, case['target_lang'])
                                               for n, t in case['candidates'].items()},
              'status': 'translation_uncertain', 'selected': None,
              'score_eligible': False, 'assessments': {}, 'reference': case['reference']}
    if rules_only:
        result['review_status'] = 'not_run'
        return result
    start = time.perf_counter()
    review_call = reviewer or (review if provider == 'ollama' else lmstudio.review)
    try:
        reviewed, metadata = review_call(case['source'], case['source_lang'], case['target_lang'], candidates, model, port)
        result.update(review_status='completed', review=reviewed, metadata=metadata)
        result['assessments'] = {aliases[r['candidate']]: r for r in reviewed['assessments']}
        for engine in case.get('candidate_order', ('baseline', 'madlad')):
            row = result['assessments'].get(engine, {})
            blocking = [f for f in result['diagnostics'].get(engine, []) if f['code'] != 'literal_html_entity']
            if row.get('verdict') == 'faithful' and not blocking:
                result.update(status='shadow_candidate', selected=engine)
                break
        correction = reviewed['corrected_translation']
        if result['selected'] is None and correction:
            checks = diagnostics(case['source'], correction, case['target_lang'])
            # New request without prior verdict or other candidates. Same reviewer,
            # NOT independent validation; results remain shadow-only.
            second, meta = review_call(case['source'], case['source_lang'], case['target_lang'],
                                  {'X': correction}, model, port)
            result['correction'] = {'text': correction, 'diagnostics': checks,
                                    'review': second, 'metadata': meta}
            if second['assessments'][0]['verdict'] == 'faithful' and not checks:
                result.update(status='shadow_candidate', selected='correction')
    except Exception as exc:
        # Failure never becomes a pass; no retries that cherry-pick verdicts.
        result.update(review_status='failed', error=f'{type(exc).__name__}: {exc}',
                      status='translation_uncertain', selected=None)
        if isinstance(exc, ReviewError):
            result['invalid_response'] = exc.envelope
    result['seconds'] = round(time.perf_counter()-start, 3)
    return result


def summarize(results):
    counts = Counter()
    for r in results:
        counts['cases'] += 1
        counts[r['review_status']] += 1
        counts[r['status']] += 1
        if r['selected']:
            counts['selected_' + r['selected']] += 1
        for engine, label in r['reference'].items():
            row = r['assessments'].get(engine)
            if label == 'material':
                counts['known_material'] += 1
                if not row:
                    counts['material_unassessed'] += 1
                elif row['verdict'] == 'material_error':
                    counts['material_detected'] += 1
                elif row['verdict'] == 'uncertain':
                    counts['material_marked_uncertain'] += 1
                else:
                    counts['material_missed'] += 1
            else:
                counts['prior_nonmaterial'] += 1
                if row and row['verdict'] != 'faithful':
                    counts['nonmaterial_rejected_by_reviewer'] += 1
            if r['selected'] == engine and label == 'material':
                counts['known_bad_selected'] += 1
        for engine, row in r['assessments'].items():
            if row['verdict'] == 'faithful' and any(f['code'] != 'literal_html_entity'
                                                   for f in r['diagnostics'][engine]):
                counts['reviewer_pass_blocked_by_rules'] += 1
    return dict(counts)


def save(work, report):
    temporary = work / 'results.tmp'
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(work / 'results.json')


def render(work, report):
    e = html.escape
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>RA automated translation review experiment</title>',
             '<style>body{font:16px/1.6 system-ui;max-width:1150px;margin:35px auto;padding:20px;color:#203040}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd5dd;padding:12px;text-align:left;vertical-align:top}section{margin:30px 0}pre{white-space:pre-wrap}small{color:#536578}</style>',
             '<h1>Automated translation review — shadow evaluation</h1>',
             '<p>Existing translator and app unchanged. No analyst translation review is required by this experimental routing: unresolved cases are automatically withheld. No result is enabled for live scoring.</p>',
             '<p>Anonymous candidates; original source supplied, previous assessments withheld. Local reviewer: '+e(report['identity']['model'])+', temperature 0. A second call checks proposed corrections, but uses the same model and is not independent evidence. Previous labels are provisional assistant judgments, not expert ground truth. This is a regression evaluation on known examples, not a held-out accuracy estimate.</p>',
             '<h2>Counts</h2><pre>'+e(json.dumps(report['summary'],indent=2))+'</pre>',
             '<table><tr><th>Case</th><th>Reviewer verdicts</th><th>Experimental routing</th><th>Diagnostics / errors</th></tr>']
    for r in report['results']:
        verdicts = '; '.join(f'{n}: {a["verdict"]}' for n,a in r['assessments'].items()) or r['review_status']
        flags = '; '.join(f'{n}: '+', '.join(f['code'] for f in fs) for n,fs in r['diagnostics'].items() if fs)
        parts.append('<tr>'+''.join('<td>'+e(str(x))+'</td>' for x in
                     (r['id'],verdicts,r['selected'] or 'Translation uncertain — withhold',flags or r.get('error','—')))+'</tr>')
    parts.append('</table><p>Publisher excerpts and their quotations are omitted here to avoid duplicating source material. See the earlier side-by-side reports and their original links. Local JSON contains the review audit trail.</p>')
    # Fictional source quotations/corrections are safe to reproduce in full.
    for r in report['results']:
        if r['kind'] != 'fictional':
            continue
        parts.append('<section><h2>'+e(r['id'])+'</h2>')
        for engine, row in r['assessments'].items():
            for issue in row['issues']:
                parts.append('<p><b>'+e(engine+' / '+issue['severity'])+'</b>: '+e(issue['explanation'])+'</p>')
        if r.get('correction'):
            parts.append('<p>Proposed correction (experimental):</p><pre>'+e(r['correction']['text'])+'</pre>')
        parts.append('</section>')
    parts.append('</html>')
    (work / 'review.html').write_text('\n'.join(parts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, default=DEFAULT)
    parser.add_argument('--model', default='qwen3:8b')
    parser.add_argument('--port', type=int, default=11435)
    parser.add_argument('--provider', choices=['ollama','lmstudio'], default='ollama')
    parser.add_argument('--rules-only', action='store_true')
    parser.add_argument('--case', action='append')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    cases = [c for c in load_cases() if not args.case or c['id'] in args.case]
    args.work.mkdir(parents=True, exist_ok=True)
    identity = {'corpus_sha256': hashlib.sha256(json.dumps(cases,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                'prompt_sha256': hashlib.sha256((PROMPT+json.dumps(SCHEMA,sort_keys=True)).encode()).hexdigest(),
                'implementation_sha256': hashlib.sha256(b''.join(p.read_bytes() for p in
                    [Path(__file__),ROOT/'scripts/translation_review/checks.py',ROOT/'scripts/translation_review/reviewer.py',ROOT/'scripts/translation_review/lmstudio.py'])).hexdigest(),
                'model': args.model, 'rules_only': args.rules_only,
                'provider': args.provider,
                'model_identity': None if args.rules_only else
                    (model_identity if args.provider=='ollama' else lmstudio.model_identity)(args.model,args.port)}
    report = {'identity': identity, 'results': []}
    if args.resume and (args.work/'results.json').exists():
        report = json.loads((args.work/'results.json').read_text())
        if report['identity'] != identity:
            raise ValueError('Cannot resume with changed corpus/prompt/model/mode')
    done = {r['id'] for r in report['results']}
    for case in cases:
        if case['id'] in done:
            continue
        result = evaluate(case,args.model,args.port,args.rules_only,args.provider)
        report['results'].append(result)
        report['summary'] = summarize(report['results'])
        save(args.work,report)
        render(args.work,report)
        print(case['id'],result['review_status'],result['selected'] or 'withheld',result.get('error',''),flush=True)
    print(json.dumps(report['summary'],indent=2),flush=True)


if __name__ == '__main__':
    main()

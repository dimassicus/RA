"""Reasoning-on regression and fresh-case evaluation; app stays untouched."""
import argparse
from functools import partial
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.evaluate_translation_review import evaluate,load_cases,save,summarize
from scripts.translation_review.fresh_cases import CASES
from scripts.translation_review.reasoning import SYSTEM,review
from scripts.translation_review.lmstudio import model_identity


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model',required=True)
    p.add_argument('--mode',choices=['on','off'],required=True)
    p.add_argument('--suite',choices=['old','fresh','all'],default='all')
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--port',type=int,default=1234)
    p.add_argument('--resume',action='store_true')
    args=p.parse_args()
    cases=(load_cases() if args.suite in ('all','old') else []) + (CASES if args.suite in ('all','fresh') else [])
    loaded=model_identity(args.model,args.port)
    if loaded['loaded_context_length']<32768:
        raise ValueError('Reasoning experiment requires 32768 context tokens')
    files=[Path(__file__),ROOT/'scripts/evaluate_translation_review.py',
           *[ROOT/'scripts/translation_review'/f for f in ('reasoning.py','fresh_cases.py','lmstudio.py','reviewer.py','checks.py')]]
    identity={'model':args.model,'mode':args.mode,'suite':args.suite,'model_identity':loaded,
              'source_corpus_sha256':hashlib.sha256(json.dumps(cases,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
              'prompt_sha256':hashlib.sha256(SYSTEM.encode()).hexdigest(),
              'implementation_sha256':hashlib.sha256(b''.join(f.read_bytes() for f in files)).hexdigest(),
              'max_output_tokens':8192,'temperature':0,'seed':42,'reasoning_effort':'medium' if args.mode=='on' else 'none',
              'transport':'unconstrained JSON final answer; strict validation afterward'}
    args.work.mkdir(parents=True,exist_ok=True)
    report={'identity':identity,'results':[]}
    if (args.work/'results.json').exists():
        if not args.resume:
            raise ValueError('Existing results: use a new directory or --resume')
        report=json.loads((args.work/'results.json').read_text())
        if report['identity']!=identity:
            raise ValueError('Resume requires unchanged inputs, prompt, implementation and model')
    (args.work/'inputs.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2))
    done={c['id'] for c in report['results']}
    for case in cases:
        if case['id'] in done:continue
        result=evaluate(case,args.model,args.port,provider='lmstudio',reviewer=partial(review,enabled=args.mode=='on'))
        report['results'].append(result)
        report['summary']=summarize(report['results'])
        save(args.work,report)
        print(case['id'],result['review_status'],result['selected'] or 'withheld',result.get('seconds'),result.get('error',''),flush=True)
    print(json.dumps(report['summary'],indent=2),flush=True)


if __name__=='__main__':main()

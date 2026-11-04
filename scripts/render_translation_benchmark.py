"""Create a local, blind review sheet from completed offline benchmark outputs."""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".local/translation-benchmark"


def main():
    models = {}
    for name in ["baseline", "nllb", "madlad"]:
        path = WORK / f"results-{name}-sentence.json"
        if path.exists():
            models[name] = json.loads(path.read_text())
    cases = json.loads((WORK / "cases.json").read_text())
    review = []
    for case in cases:
        if case["kind"] != "fictional_article":
            continue
        candidates = []
        for model, dataset in models.items():
            result = next((r for r in dataset["results"] if r["id"] == case["id"]), None)
            if result and result["status"] == "ok":
                candidates.append({"model": model, "text": result["translation"], "seconds": result["seconds"]})
        # Stable per-case shuffling makes labels uninformative across articles.
        candidates.sort(key=lambda r: hashlib.sha256((case["id"] + r["model"]).encode()).hexdigest())
        review.append({**case, "candidates": candidates})
    payload = json.dumps(review, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    page = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RA — Translation review</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#172b3a;font:16px/1.55 system-ui,sans-serif}
main{max-width:1600px;margin:auto;padding:32px}h1{font-size:30px;margin-bottom:4px}h2{font-size:20px}p{margin-top:8px}
.muted{color:#536779}.notice{background:#fff2d8;border-left:4px solid #bc8a25;padding:14px 18px;margin:20px 0}
.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:22px 0}button,select,textarea{font:inherit;border:1px solid #b6c5d0;border-radius:6px;padding:10px;background:white;color:inherit}
button{cursor:pointer}button.primary{background:#17476a;color:white}.source,.card{background:white;border:1px solid #d9e1e8;border-radius:10px;padding:22px}
.source{margin-bottom:22px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}.text{white-space:pre-wrap;line-height:1.65}
.checks{margin:18px 0;padding:14px 22px;background:#eaf0f5;border-radius:8px}label{display:block;margin-top:14px}textarea{width:100%;min-height:120px}select{max-width:100%}
.model{font-size:14px;color:#536779}.status{font-size:14px;color:#22663c}details{margin:16px 0}footer{margin-top:32px;font-size:14px;color:#536779}
@media print{.toolbar,button,textarea,select{display:none}.grid{display:block}.card{break-inside:avoid;margin-bottom:15px}}
</style>
<main><h1>RA translation review</h1><p class="muted">Three controlled credit-news articles · Russian ↔ English · local model comparison</p>
<div class="notice"><strong>All three articles are fictional test fixtures.</strong> They are not news or allegations about real companies. No translations have been applied to the running RA app.</div>
<p>Judge factual fidelity before style. A fluent translation that reverses a negation, changes an amount, drops a condition, or assigns an event to the wrong company fails the test. Candidates use the same sentence boundaries. English → Russian has two candidates because the existing app only translates into English.</p>
<div class="toolbar"><label for="case" style="margin:0">Article / direction</label><select id="case"></select><button id="reveal">Reveal model names</button><button class="primary" id="export">Export my review</button><span class="status" id="status"></span></div>
<section class="source"><h2 id="heading"></h2><div class="text" id="source"></div></section>
<details class="checks" open><summary>Facts and legal distinctions to check</summary><ul id="checks"></ul></details>
<section class="grid" id="candidates"></section>
<footer>Notes are saved in this browser when local storage is available. Export the review JSON to keep a portable copy. No data is sent anywhere. Model identities are merely hidden for convenience; this is not a formally blinded study. These results are an exploratory test, not an accuracy estimate.</footer></main>
<script>
const data = PAYLOAD;
const key='ra-translation-review-v1';
let notes={}, revealed=false;
try{notes=JSON.parse(localStorage.getItem(key)||'{}')}catch(e){}
const selector=document.getElementById('case');
data.forEach((c,i)=>{const o=document.createElement('option');o.value=i;o.textContent=`${c.title} · ${c.source_lang.toUpperCase()} → ${c.target_lang.toUpperCase()}`;selector.append(o)});
function save(){try{localStorage.setItem(key,JSON.stringify(notes));document.getElementById('status').textContent='Review saved in this browser'}catch(e){document.getElementById('status').textContent='Use Export to save your review'}}
const labels={baseline:'Existing Argos/OPUS translator',nllb:'Meta NLLB 1.3B · CT2 INT8',madlad:'Google MADLAD 3B · CT2 INT8'};
function draw(){const c=data[Number(selector.value)]; document.getElementById('heading').textContent=c.title+' — source';document.getElementById('source').textContent=c.paragraphs.join('\n\n');
const list=document.getElementById('checks');list.replaceChildren();c.checks.forEach(t=>{const li=document.createElement('li');li.textContent=t;list.append(li)});
const grid=document.getElementById('candidates');grid.replaceChildren();
c.candidates.forEach((r,i)=>{const id=c.id+':'+r.model;const card=document.createElement('article');card.className='card';const h=document.createElement('h2');h.textContent='Candidate '+String.fromCharCode(65+i);card.append(h);
if(revealed){const m=document.createElement('p');m.className='model';m.textContent=labels[r.model];card.append(m)}
const text=document.createElement('div');text.className='text';text.textContent=r.text;card.append(text);
const label=document.createElement('label');label.textContent='Fidelity assessment';const score=document.createElement('select');
[['','Not reviewed'],['pass','Pass — all material meaning retained'],['minor','Minor issues — meaning retained'],['major','Major error or omission'],['critical','Critical — changes the risk conclusion']].forEach(([v,t])=>{const o=document.createElement('option');o.value=v;o.textContent=t;score.append(o)});
score.value=(notes[id]||{}).rating||'';label.append(score);card.append(label);
const nl=document.createElement('label');nl.textContent='Errors, missing facts, and your preferred wording';const note=document.createElement('textarea');note.value=(notes[id]||{}).notes||'';nl.append(note);card.append(nl);
function update(){notes[id]={case_id:c.id,model:r.model,candidate:String.fromCharCode(65+i),rating:score.value,notes:note.value};save()}
score.addEventListener('change',update);note.addEventListener('input',update);grid.append(card)});
}
selector.addEventListener('change',draw);document.getElementById('reveal').onclick=()=>{revealed=!revealed;document.getElementById('reveal').textContent=revealed?'Hide model names':'Reveal model names';draw()};
document.getElementById('export').onclick=()=>{const blob=new Blob([JSON.stringify({schema:'ra-translation-human-review-v1',exported_at:new Date().toISOString(),reviews:Object.values(notes)},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='ra-translation-review.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};
draw();
</script></html>'''.replace("PAYLOAD", payload)
    out = ROOT / "output" / "translation-benchmark-review.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(page)
    print(out)
    # Full machine-readable results are also copied beside the review sheet.
    (out.parent / "translation-benchmark-results.json").write_text(json.dumps(models, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

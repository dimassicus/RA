"""Conservative, deterministic grouping of repeated company coverage."""
import re
from collections import defaultdict
from datetime import timedelta

from .article_content import canonical_url

VERSION = 'event-group-v1'
FILLER = set('the a an has had have was is were been its said says announced reports reported according to and in on at of for as by from with that this it'.split())


def same_event(a, b):
    if canonical_url(a['url']) == canonical_url(b['url']):
        return True
    va, vb = a.get('verification') or {}, b.get('verification') or {}
    if va.get('status') != 'supported' or vb.get('status') != 'supported':
        return False
    if va.get('document_hash') and va['document_hash'] == vb.get('document_hash'):
        return True
    if va.get('source_url') and canonical_url(va['source_url']) == canonical_url(vb.get('source_url') or b['url']):
        return True
    if not a.get('published_at') or not b.get('published_at'):
        return False
    if abs(a['published_at'] - b['published_at']) > timedelta(days=3):
        return False
    if {(e['rule'], e['score']) for e in va.get('evidence', [])} != {(e['rule'], e['score']) for e in vb.get('evidence', [])}:
        return False
    sa, sb = va.get('signature', ''), vb.get('signature', '')
    # Different amounts, dates, quarters or case numbers are separate developments.
    if re.findall(r'\d+', sa) != re.findall(r'\d+', sb):
        return False
    ta, tb = set(sa.split()), set(sb.split())
    if min(len(ta), len(tb)) < 8:
        return False
    return (ta - FILLER == tb - FILLER) and len(ta & tb) / len(ta | tb) >= 0.9


def assign_groups(articles):
    groups = defaultdict(list)
    assigned = {}
    ai_urls = {(a.get('company_id') or ('oneoff', a['run_id']), canonical_url(a['url'])): a['event_draft']['event']
               for a in sorted(articles, key=lambda a: a['id']) if (a.get('event_draft') or {}).get('event')}
    manual_urls = {(a.get('company_id') or ('oneoff', a['run_id']), canonical_url(a['url'])): a['event_group_manual']
                   for a in sorted(articles, key=lambda a: a['id']) if a.get('event_group_manual')}
    for article in sorted(articles, key=lambda a: a['id']):
        company = article.get('company_id') or ('oneoff', article['run_id'])
        manual = manual_urls.get((company, canonical_url(article['url'])))
        if manual:
            assigned[article['id']] = manual
            continue
        ai = ai_urls.get((company, canonical_url(article['url'])))
        if ai:
            assigned[article['id']] = f"event:{ai}"
            continue
        # Compare against the representative only, avoiding transitive chain merges.
        for representative in groups[company]:
            if same_event(article, representative):
                assigned[article['id']] = assigned[representative['id']]
                break
        else:
            groups[company].append(article)
            assigned[article['id']] = f"source:{article['id']}"
    return assigned


def event_assessment(members, *, saved=False):
    """Count distinct documents. A repeated discovery never adds weight."""
    articles = [r for r in members if not saved or r.get('report_selected')]
    parents = {}

    def root(key):
        parents.setdefault(key, key)
        while key != parents[key]:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    for row in articles:
        check = row.get('verification') or {}
        key = root(canonical_url(row['url']))
        if check.get('source_url'):
            parents[root(canonical_url(check['source_url']))] = key
        if check.get('document_hash'):
            parents[root(('hash', check['document_hash']))] = key
    unique, duplicates = {}, []
    for row in sorted(articles, key=lambda r: (r.get('score_status') == 'reviewed',
                      str(r.get('score_updated_at') or r.get('created_at') or ''), r['id']), reverse=True):
        key = root(canonical_url(row['url']))
        if key in unique:
            duplicates.append(row['id'])
        else:
            unique[key] = row
    sources = [r for r in unique.values() if r.get('score') in (-1, 0)
               and (not saved or r.get('score_included'))
               and (r.get('score_status') == 'reviewed' or not (r.get('event_draft') or {}).get('excluded'))]
    adverse = [r for r in sources if r['score'] < 0]
    material = any(any(e.get('rule') in ('default or insolvency', 'material financial deterioration',
                       'operating disruption', 'legal or creditor pressure') for e in r.get('score_evidence', []))
                   for r in adverse)
    # A material negative overrides neutral coverage. Otherwise use a corroboration-weighted vote.
    weight = len(adverse) + max(0, len(adverse) - 1)
    score = None if not sources else -1 if adverse and (material or weight >= len(sources) - len(adverse)) else 0
    reason = (f"{len(adverse)} negative and {len(sources)-len(adverse)} neutral distinct sources."
              + (" Material adverse evidence takes precedence." if material else ""))
    return dict(score=score, sources=sources, duplicate_ids=duplicates, reason=reason,
                priority=(material, weight, len(sources)))

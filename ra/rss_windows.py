"""Successful Google News RSS coverage for repeat watchlist searches."""
from collections import defaultdict
import json
from datetime import timedelta


TICK = timedelta(microseconds=1)


def load(conn, keys, window):
    covered = defaultdict(list)
    for row in conn.execute("""SELECT subject_key,query,edition,start_at,end_at FROM rss_search_windows
            WHERE (subject_key=ANY(%s) OR subject_key LIKE 'topic:%%') AND start_at<=%s AND end_at>=%s ORDER BY start_at""",
            (keys, window[1], window[0])):
        covered[row['subject_key'], row['query'], row['edition']].append((row['start_at'], row['end_at']))
    return covered


def gaps(window, covered):
    start, end = window
    for left, right in sorted(covered):
        if right < start:
            continue
        if left > end:
            break
        if left > start:
            yield start, left - TICK
        start = max(start, right + TICK)
        if start > end:
            return
    if start <= end:
        yield start, end


def save(conn, run_id, entries):
    if entries:
        with conn.cursor() as cursor:
            cursor.executemany("""INSERT INTO rss_search_windows(run_id,subject_key,query,edition,start_at,end_at)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                [(run_id, key, query, edition, *span) for key, query, edition, span in entries])

def topic_key(branch, topic, factor):
    return 'topic:' + json.dumps([branch, topic, factor], ensure_ascii=False, separators=(',', ':'))

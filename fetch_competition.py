#!/usr/bin/env python3
"""
Scrape cubing.com competition results and build per-competitor folders.

Folder layout:
  <out>/<YYMMDD> <Competition Name>/<Person>/<Event> <Round> <Avg> avg/attempts.txt

Round labels follow round count: 4 -> R1 R2 R3 Fi, 3 -> R1 R2 Fi, 2 -> R1 Fi, 1 -> Fi.

Usage:
  python fetch_competition.py Chengdu-Welcoming-Summer-2026
  python fetch_competition.py Chengdu-Welcoming-Summer-2026 --events 333 --dry-run
  python fetch_competition.py <slug> --all-people    # all competitors, not only matched
"""
import argparse
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import websocket  # pip install websocket-client

WS_URL = "wss://cubing.com/ws"
LIVE_URL = "https://cubing.com/live/{slug}?lang=en"
WCA_URL = "https://www.worldcubeassociation.org/api/v0/competitions/{id}"

EVENT_NAME = {
    '333': '3x3', '222': '2x2', '444': '4x4', '555': '5x5',
    '666': '6x6', '777': '7x7',
    '333oh': '3x3 OH', '333bf': '3x3 BLD', '333fm': '3x3 FM',
    '333ft': '3x3 Feet', '333mbf': '3x3 MBLD',
    '444bf': '4x4 BLD', '555bf': '5x5 BLD',
    'pyram': 'Pyra', 'skewb': 'Skewb', 'minx': 'Minx',
    'sq1': 'Square-1', 'clock': 'Clock',
}


def http_get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    return urllib.request.urlopen(req, timeout=15).read().decode('utf-8')


def fetch_live_meta(slug):
    body = http_get(LIVE_URL.format(slug=slug))
    m_c = re.search(r'data-c="(\d+)"', body)
    if not m_c:
        raise RuntimeError("competitionId (data-c) not found")
    m_ev = re.search(r'data-events="([^"]+)"', body)
    if not m_ev:
        raise RuntimeError("data-events not found")
    events = json.loads(html.unescape(m_ev.group(1)))
    m_t = re.search(r'<title>([^<]+)</title>', body)
    title = m_t.group(1).split(' - ')[0].strip() if m_t else slug
    return int(m_c.group(1)), events, title


def fetch_wca_date(slug):
    wca_id = slug.replace('-', '')
    try:
        data = json.loads(http_get(WCA_URL.format(id=wca_id)))
        return data.get('start_date')
    except Exception as e:
        print(f"  WCA date fetch failed: {e}", file=sys.stderr)
        return None


def yymmdd(date_str):
    if not date_str:
        return 'XXXXXX'
    y, m, d = date_str.split('-')
    return y[2:] + m + d


def format_time(cs):
    if cs is None or cs == 0:
        return 'DNS'
    if cs < 0:
        return 'DNF'
    if cs < 6000:
        return f"{cs/100:.2f}"
    m, s = divmod(cs, 6000)
    return f"{m}:{s/100:05.2f}"


def format_summary(avg, best):
    """Return the '<X> avg' / '<X> best' / 'DNF avg' suffix for the folder name."""
    if avg and avg > 0:
        return f"{format_time(avg)} avg"
    if avg == -1:
        return "DNF avg"
    if best and best > 0:
        return f"{format_time(best)} best"
    return "DNF avg"


def trimmed_attempts(v):
    """Drop trailing zeros (un-attempted); keep DNFs in place."""
    out = list(v or [])
    while out and out[-1] == 0:
        out.pop()
    return [format_time(x) for x in out]


def round_label(num_rounds, index):
    return 'Fi' if index == num_rounds - 1 else f'R{index + 1}'


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*]', '_', s).strip().rstrip('.')


def load_person_map(person_dir):
    """Return key -> (folder_name, events_set | None). None means "no event.txt -> accept all"."""
    pdir = Path(person_dir)
    if not pdir.is_dir():
        return {}
    m = {}
    for d in pdir.iterdir():
        if not d.is_dir():
            continue
        name = d.name
        # 仅当首字符 ASCII 字母 + 第二字符非 ASCII(分组前缀紧跟 CJK)才剥前缀,
        # 否则 `Timofei Tarasenko` / `Max Park` / `Feliks Zemdegs` 会被错误截首字母
        if (len(name) >= 2 and name[0].isascii() and name[0].isalpha()
                and not name[1].isascii()):
            key = name[1:]
        else:
            key = name
        ev_file = d / 'event.txt'
        events = None
        if ev_file.exists():
            events = {ln.strip() for ln in ev_file.read_text(encoding='utf-8').splitlines() if ln.strip()}
        m[key.strip()] = (name, events)
    return m


def match_key(competitor_name):
    m = re.search(r'\(([^)]+)\)', competitor_name)
    return (m.group(1) if m else competitor_name).strip()


def ws_fetch(cid, requests):
    """requests: list of (event, round). Returns (users, {(e,r): [results]})."""
    ws = websocket.create_connection(
        WS_URL, timeout=15,
        origin="https://cubing.com",
        header=["User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"],
    )
    ws.settimeout(15)
    users = {}
    results = {(e, r): [] for e, r in requests}
    pending_count = len(requests)
    received_results = 0
    got_users = False

    ws.send(json.dumps({'type': 'competition', 'competitionId': cid}))
    for e, r in requests:
        ws.send(json.dumps({
            'type': 'result', 'action': 'fetch',
            'params': {'event': e, 'round': r, 'filter': 'all'},
        }))

    deadline = time.time() + 30
    while time.time() < deadline:
        if got_users and received_results >= pending_count:
            break
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            break
        except Exception:
            break
        if not raw:
            continue
        msg = json.loads(raw)
        t = msg.get('type')
        if t == 'users':
            for k, v in msg.get('data', {}).items():
                users[int(k)] = v
            got_users = True
        elif t == 'result.all':
            data = msg.get('data', [])
            received_results += 1
            if data:
                key = (data[0]['e'], data[0]['r'])
                if key in results:
                    results[key] = data
    ws.close()
    return users, results


def main():
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument('slug', nargs='+',
                    help='Competition slug, e.g. Chengdu-Welcoming-Summer-2026 (spaces auto-join with hyphens)')
    ap.add_argument('--out', default=r'Z:\魔方比赛', help='Output root')
    ap.add_argument('--events', default='', help='Comma-separated event ids (default: all)')
    ap.add_argument('--person', default=r'D:\cube\video-by-face\person', help='Person dir')
    ap.add_argument('--all-people', action='store_true',
                    help='Create folders for every competitor, not only those in --person')
    ap.add_argument('--ignore-event-filter', action='store_true',
                    help='Ignore each person\'s event.txt and process every event')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    slug = '-'.join(args.slug)
    print(f"Live page: {slug}")
    cid, events, title = fetch_live_meta(slug)
    print(f"  competitionId={cid}, title={title}")

    date = fetch_wca_date(slug)
    folder_date = yymmdd(date)
    print(f"  date: {date} -> {folder_date}")

    person_map = load_person_map(args.person)
    print(f"  {len(person_map)} person folders loaded")

    selected = [e['i'] for e in events]
    if args.events:
        wanted = set(args.events.split(','))
        selected = [e for e in selected if e in wanted]
    print(f"  events: {selected}")

    rounds_by_event = {e['i']: [r['i'] for r in e['rs']] for e in events if e['i'] in selected}
    requests = [(eid, rid) for eid, rs in rounds_by_event.items() for rid in rs]
    print(f"  fetching {len(requests)} rounds via WebSocket ...")

    users, results = ws_fetch(cid, requests)
    print(f"  got {len(users)} competitors, {sum(1 for v in results.values() if v)} non-empty rounds")

    comp_folder = Path(args.out) / safe_name(f"{folder_date} {title}")
    created = 0

    for eid in selected:
        rounds = rounds_by_event[eid]
        event_label = EVENT_NAME.get(eid, eid)
        for idx, rid in enumerate(rounds):
            rlist = results.get((eid, rid)) or []
            if not rlist:
                continue
            r_label = round_label(len(rounds), idx)
            for r in rlist:
                user = users.get(r['n'])
                if not user:
                    continue
                cname = user['name']
                key = match_key(cname)
                entry = person_map.get(key)
                if entry:
                    folder, ev_set = entry
                    if (not args.ignore_event_filter) and ev_set is not None and eid not in ev_set:
                        continue
                elif args.all_people:
                    folder = safe_name(key)
                else:
                    continue
                summary = format_summary(r.get('a'), r.get('b'))
                attempts = trimmed_attempts(r.get('v', []))
                if not attempts:
                    continue
                round_dir = comp_folder / folder / safe_name(f"{event_label} {r_label} {summary}")
                if args.dry_run:
                    print(f"  DRY {round_dir}")
                else:
                    round_dir.mkdir(parents=True, exist_ok=True)
                    (round_dir / 'attempts.txt').write_text(
                        '\n'.join(attempts) + '\n', encoding='utf-8')
                created += 1

    verb = 'Would create' if args.dry_run else 'Created'
    print(f"{verb} {created} round folders under: {comp_folder}")
    print(f"Elapsed: {time.time() - t0:.2f}s")


if __name__ == '__main__':
    main()

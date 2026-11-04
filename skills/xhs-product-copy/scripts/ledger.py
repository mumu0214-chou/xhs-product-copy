#!/usr/bin/env python3
"""Shared XHS ledger v2. Standard library; run --help for commands."""
import argparse
import copy
from contextlib import contextmanager, nullcontext
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import urlparse

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

VERSION = 2
GROUPS = ('sources', 'templates', 'products', 'uses', 'tasks', 'drafts', 'feedback_records')
DEFAULT_ROOT = Path.home() / 'Documents/Codex/小红书笔记台账'


@contextmanager
def ledger_lock(path, timeout=30.0):
    """An OS-owned lock: Windows byte-range lock, Unix flock; released on exit/crash."""
    with path.open('a+b', buffering=0) as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                lock.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'Ledger is busy: {path}; retry the same event later') from error
                time.sleep(0.05)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def nonempty(value, label):
    require(isinstance(value, str) and bool(value.strip()), f'{label}: nonempty string required')
    return value


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', value), 'Invalid ID')
    return value


def number(value, label):
    require(type(value) is int and value >= 0, f'{label}: nonnegative integer required')
    return value


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def empty():
    return {'schema_version': VERSION, **{k: {} for k in GROUPS}, 'events': []}


def migrate(data):
    """Preserve all legacy fields, counts, and imprecise evidence without fabricating drafts."""
    result = copy.deepcopy(data)
    require(result.get('schema_version') in (1, VERSION), 'Unsupported schema_version')
    if result['schema_version'] == 1:
        result['schema_version'] = VERSION
        result['migration'] = {'from': 1, 'note': 'Legacy counts and chat locations retained; no invented draft IDs.'}
    for group in GROUPS:
        result.setdefault(group, {})
    return result


def validate(data):
    require(data.get('schema_version') == VERSION, 'Unsupported schema')
    for group in GROUPS:
        require(isinstance(data.get(group), dict), f'{group} must be object')
        for key, value in data[group].items():
            require(isinstance(value, dict) and value.get('id') == key, f'{group}/{key}: ID mismatch')
    for use in data['uses'].values():
        require(use.get('product_id') in data['products'], 'Unknown product reference')
        for field, group in [('source_id', 'sources'), ('template_id', 'templates')]:
            require(use.get(field) is None or use[field] in data[group], f'Unknown {field}')
        number(use.get('variants', {}).get('count', 0), 'variant count')
        if use.get('task_id'):
            require(use['task_id'] in data['tasks'], 'Unknown task reference')
    for draft in data['drafts'].values():
        require(draft['use_id'] in data['uses'], 'Unknown draft use')
        require(draft['kind'] in ('mother', 'variant'), 'Invalid draft kind')
        require(str(draft['latest']) in draft['versions'], 'Missing latest version')
    ids = [e.get('id') for e in data['events']]
    require(all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids), 'Duplicate or missing event IDs')


def canonical(source):
    parsed = urlparse(nonempty(source.get('url'), 'source.url'))
    require(parsed.scheme in ('https', 'http') and (parsed.hostname or '').lower() in ('www.xiaohongshu.com', 'xiaohongshu.com'), 'Use resolved Xiaohongshu URL')
    match = re.search(r'/(?:explore|discovery/item)/([a-fA-F0-9]{24})(?:/|$)', parsed.path)
    require(match is not None, 'Resolved note URL must contain note ID')
    sid = match.group(1).lower()
    require(source.get('id', sid).lower() == sid, 'Source ID differs from URL')
    return sid, 'https://www.xiaohongshu.com/explore/' + sid


def latest_feedback(data, target, target_id, revision=None):
    matches = [f for f in data['feedback_records'].values()
               if f['target'] == target and f['target_id'] == target_id and f.get('revision') == revision]
    return matches[-1] if matches else {'status': 'unknown'}


def mother_approved(data, use):
    if use.get('managed_v2'):
        d = data['drafts'].get(use['id'] + '--mother')
        return bool(d and latest_feedback(data, 'draft', d['id'], d['latest'])['status'] == 'approved')
    # Legacy approval scope is not silently reinterpreted as mother approval.
    return False


def counts(data):
    uses = list(data['uses'].values())
    return {'sources': len(data['sources']), 'templates': len(data['templates']),
            'mothers': sum(u.get('rewrite', {}).get('status') == 'done' for u in uses),
            'variants': sum(u.get('variants', {}).get('count', 0) for u in uses),
            'approved_mothers_v2': sum(mother_approved(data, u) for u in uses),
            'legacy_approved_uses': sum(not u.get('managed_v2') and u.get('feedback', {}).get('status') == 'approved' for u in uses)}


def sync_use(data, uid):
    use = data['uses'][uid]
    drafts = [d for d in data['drafts'].values() if d['use_id'] == uid]
    mother = next((d for d in drafts if d['kind'] == 'mother'), None)
    variants = [d for d in drafts if d['kind'] == 'variant']
    if mother:
        v = mother['versions'][str(mother['latest'])]
        use['rewrite'] = {'status': 'done', 'title': v['title'], 'completed_at': v['created_at'], 'output_location': v['artifact'], 'draft_id': mother['id'], 'revision': mother['latest']}
    use['variants'] = {'status': 'done' if variants else 'pending', 'count': len(variants), 'draft_ids': [d['id'] for d in variants]}


def progress(data, tid):
    task = data['tasks'][tid]
    uses = [u for u in data['uses'].values() if u.get('task_id') == tid and u.get('status') != 'withdrawn']
    actual = {'selections': sum(u.get('source_id') is not None for u in uses),
              'mothers': sum(u.get('rewrite', {}).get('status') == 'done' for u in uses),
              'variants_by_use': {u['id']: u.get('variants', {}).get('count', 0) for u in uses}}
    expected = task['expected']
    enough = actual['selections'] >= expected['selections'] and actual['mothers'] >= expected['mothers']
    enough = enough and all(actual['variants_by_use'].get(uid, 0) >= n for uid, n in expected['variants_by_use'].items())
    return {'actual': actual, 'expected': expected, 'requirements_met': enough, 'status': task['status']}


def draft_text(version):
    return '# ' + version['title'] + '\n\n' + version['body'].strip() + '\n'


def reduce(data, request):
    """Apply exactly one identified operation; caller holds the exclusive lock."""
    eid = identifier(request.get('event_id'))
    payload_hash = digest(encoded(request))
    prior = next((e for e in data['events'] if e['id'] == eid), None)
    if prior:
        require(prior.get('request_hash') == payload_hash, 'Event ID conflict; use a new ID for a changed request')
        return data, False
    result = copy.deepcopy(data)
    op = request.get('op')
    stamp = now()
    q = request
    tid = q.get('task_id')
    if op == 'start':
        identifier(tid)
        require(tid not in result['tasks'], 'Task already exists; reuse its ID without starting again')
        product = q['product']
        pid = identifier(product['id'])
        nonempty(product.get('name'), 'product.name')
        require(isinstance(q.get('brief'), dict) and q['brief'].get('facts'), 'Task brief.facts required')
        expected = q['expected']
        require(set(expected) == {'selections', 'mothers', 'variants_by_use'}, 'Expected selections/mothers/variants_by_use required')
        number(expected['selections'], 'selections'); number(expected['mothers'], 'mothers')
        require(isinstance(expected['variants_by_use'], dict), 'variants_by_use must be object')
        for uid, n in expected['variants_by_use'].items():
            identifier(uid); number(n, 'variant target')
        if pid in result['products']:
            require(result['products'][pid]['name'] == product['name'], 'Product name conflict')
        else:
            result['products'][pid] = {'id': pid, 'name': product['name']}
        result['tasks'][tid] = {'id': tid, 'product_id': pid, 'brief': q['brief'], 'expected': expected, 'status': 'in_progress', 'created_at': stamp}
    elif op in ('select', 'original'):
        task = result['tasks'][tid]
        uid = identifier(q['use_id'])
        require(uid not in result['uses'], 'Use already exists; retry original event or revise existing draft')
        sid = template_id = None
        selection = q.get('selection', {})
        if op == 'select':
            source = copy.deepcopy(q['source'])
            sid, url = canonical(source)
            for key in ('title', 'source_text', 'read_at', 'provenance'):
                nonempty(source.get(key), 'source.' + key)
            require(source.get('reading_scope') in ('full_text', 'caption_only', 'title_cover_only'), 'Invalid reading scope')
            source.update(id=sid, canonical_url=url)
            bodyhash = digest(re.sub(r'\s+', '', source['source_text']))
            source['text_hash'] = bodyhash
            for existing in result['sources'].values():
                if existing['id'] != sid and existing.get('text_hash') == bodyhash:
                    raise ValueError('Duplicate source text under a different note ID')
            already = sid in result['sources'] and (result['sources'][sid].get('first_delivered_at') or any(u.get('source_id') == sid for u in result['uses'].values()))
            require(not already or q.get('reuse_reason'), 'Source already delivered/used; explicit reuse_reason required')
            old = result['sources'].get(sid, {})
            if old.get('source_text'):
                require(old['source_text'] == source['source_text'], 'Source text changed; preserve old reading and resolve before selection')
            result['sources'][sid] = {**old, **source, 'first_delivered_at': old.get('first_delivered_at', stamp)}
            template = q['template']; template_id = identifier(template['id'])
            nonempty(template.get('name'), 'template.name'); nonempty(template.get('structure'), 'template.structure')
            if template_id not in result['templates']:
                result['templates'][template_id] = {**template, 'review_status': '新候选'}
            result['templates'][template_id]['last_observed_at'] = source['read_at']
            for key in ('borrowed_excerpt', 'attraction', 'product_bridge', 'non_transferable', 'pattern', 'source_voice'):
                nonempty(selection.get(key), 'selection.' + key)
            require(selection['source_voice'] in ('merchant', 'consumer', 'unclear'), 'Invalid source voice')
            if selection['source_voice'] == 'consumer':
                nonempty(selection.get('role_conversion'), 'consumer role_conversion')
        else:
            nonempty(q.get('topic'), 'original topic')
            selection = {'topic': q['topic'], 'source_voice': 'original'}
        result['uses'][uid] = {'id': uid, 'managed_v2': True, 'task_id': tid, 'source_id': sid, 'template_id': template_id, 'product_id': task['product_id'], 'selection': selection, 'reuse_reason': q.get('reuse_reason'), 'rewrite': {'status': 'pending'}, 'variants': {'status': 'pending', 'count': 0}, 'feedback': {'status': 'unknown'}, 'publication': {'status': 'unknown'}}
        task['status'] = 'in_progress'
    elif op == 'withdraw':
        use = result['uses'][q['use_id']]
        require(use.get('task_id') == tid, 'Use belongs to another task')
        require(not any(d['use_id'] == use['id'] for d in result['drafts'].values()), 'Cannot withdraw completed writing; retain history and start a new use')
        nonempty(q.get('reason'), 'withdraw reason')
        use.update(status='withdrawn', withdraw_reason=q['reason'])
        result['tasks'][tid]['status'] = 'in_progress'
    elif op == 'plan':
        task = result['tasks'][tid]; expected = q['expected']
        nonempty(q.get('reason'), 'plan reason')
        require(set(expected) == {'selections', 'mothers', 'variants_by_use'}, 'Invalid plan fields')
        number(expected['selections'], 'selections'); number(expected['mothers'], 'mothers')
        require(isinstance(expected['variants_by_use'], dict), 'variants_by_use must be object')
        for uid, n in expected['variants_by_use'].items():
            identifier(uid); number(n, 'variant target')
        task.update(expected=expected, status='in_progress')
    elif op == 'drafts':
        task = result['tasks'][tid]
        require(isinstance(q.get('drafts'), list) and q['drafts'], 'Nonempty drafts required')
        require(len({d['id'] for d in q['drafts']}) == len(q['drafts']), 'Repeated draft ID in batch')
        for item in q['drafts']:
            did = identifier(item['id']); uid = item['use_id']; use = result['uses'][uid]
            require(use.get('managed_v2') and use.get('task_id') == tid and use.get('status') != 'withdrawn', 'Draft belongs to another task, withdrawn or legacy use')
            require(item.get('kind') in ('mother', 'variant'), 'Invalid draft kind')
            if item['kind'] == 'mother':
                require(did == uid + '--mother', 'Mother ID must be use_id--mother')
            else:
                require(did.startswith(uid + '--v'), 'Variant ID must start use_id--v')
                mother = result['drafts'].get(uid + '--mother')
                require(mother is not None, 'Write mother before variants')
                require(str(item.get('mother_revision')) in mother['versions'], 'Unknown mother revision')
            old = result['drafts'].get(did)
            expected_revision = number(item.get('expected_revision'), 'expected_revision')
            require(expected_revision == (old['latest'] if old else 0), 'Revision conflict; reload current draft before editing')
            if old:
                require(old['kind'] == item['kind'] and old['use_id'] == uid, 'Draft identity cannot change')
            nonempty(item.get('title'), 'draft.title'); nonempty(item.get('body'), 'draft.body')
            version = {'title': item['title'], 'body': item['body'], 'created_at': stamp, 'event_id': eid}
            if item['kind'] == 'variant':
                version['mother_revision'] = item['mother_revision']
            text = draft_text(version)
            # Exact duplicate variants are not counted as new output.
            for other in result['drafts'].values():
                if other['use_id'] == uid and other['id'] != did:
                    require(other['versions'][str(other['latest'])].get('content_hash') != digest(text), 'Duplicate draft content')
            version['content_hash'] = digest(text)
            version['artifact'] = 'artifacts/' + version['content_hash'] + '.md'
            revision = expected_revision + 1
            draft = old or {'id': did, 'use_id': uid, 'kind': item['kind'], 'versions': {}}
            draft['versions'][str(revision)] = version; draft['latest'] = revision
            result['drafts'][did] = draft
            sync_use(result, uid)
        task['status'] = 'in_progress'
    elif op == 'feedback':
        nonempty(q.get('quote'), 'user quote'); nonempty(q.get('evidence'), 'feedback evidence')
        require(q.get('status') in ('approved', 'rejected', 'revise'), 'Invalid feedback status')
        require(q.get('targets'), 'Explicit feedback targets required')
        for i, target in enumerate(q['targets']):
            kind = target['kind']; target_id = target['id']; revision = target.get('revision')
            group = {'draft': 'drafts', 'selection': 'uses', 'template': 'templates', 'source': 'sources'}.get(kind)
            require(group and target_id in result[group], 'Unknown feedback target')
            if kind == 'draft':
                require(type(revision) is int and str(revision) in result['drafts'][target_id]['versions'], 'Feedback requires exact draft revision')
            else:
                require(revision is None, 'Only draft feedback has a revision')
            fid = eid + '--' + str(i + 1)
            result['feedback_records'][fid] = {'id': fid, 'target': kind, 'target_id': target_id, 'revision': revision, 'status': q['status'], 'quote': q['quote'], 'evidence': q['evidence'], 'created_at': stamp}
    elif op == 'review-template':
        template = result['templates'][q['template_id']]
        require(q.get('status') in ('新候选', '认可过待复查', '本轮适用', '降低优先级'), 'Invalid template review status')
        nonempty(q.get('reason'), 'review reason'); nonempty(q.get('evidence'), 'review evidence')
        template.update(review_status=q['status'], review_reason=q['reason'], last_search_review_at=stamp)
    elif op == 'publication':
        draft = result['drafts'][q['draft_id']]
        require(str(q.get('revision')) in draft['versions'], 'Unknown publication revision')
        nonempty(q.get('evidence'), 'publication evidence')
        # Reporting an existing publication only; this command does not publish anything.
        draft['versions'][str(q['revision'])]['publication'] = {'status': 'published', 'evidence': q['evidence'], 'recorded_at': stamp}
    elif op == 'finish':
        task = result['tasks'][tid]
        p = progress(result, tid)
        if p['requirements_met']:
            task['status'] = 'complete'
        else:
            nonempty(q.get('reason'), 'partial task reason')
            task['status'] = 'partial'; task['reason'] = q['reason']
    else:
        raise ValueError('Unknown operation')
    result['events'].append({'id': eid, 'type': op, 'task_id': tid, 'created_at': stamp, 'request_hash': payload_hash, 'request': q})
    validate(result)
    return result, True


def handoff(data, tid):
    task = data['tasks'][tid]
    uses = [u for u in data['uses'].values() if u.get('task_id') == tid]
    return {'schema_version': VERSION, 'task': task, 'progress': progress(data, tid), 'uses': uses,
            'sources': {u['source_id']: data['sources'][u['source_id']] for u in uses if u.get('source_id')},
            'templates': {u['template_id']: data['templates'][u['template_id']] for u in uses if u.get('template_id')},
            'drafts': {d['id']: d for d in data['drafts'].values() if d['use_id'] in {u['id'] for u in uses}},
            'feedback': [f for f in data['feedback_records'].values() if f['target_id'] in {u['id'] for u in uses} | {d['id'] for d in data['drafts'].values() if d['use_id'] in {u['id'] for u in uses}}]}


def query(data):
    excluded = {u['source_id'] for u in data['uses'].values() if u.get('source_id')}
    excluded.update(s['id'] for s in data['sources'].values() if s.get('first_delivered_at'))
    templates = []
    for template in data['templates'].values():
        uses = [u for u in data['uses'].values() if u.get('template_id') == template['id']]
        templates.append({**template, 'mother_uses': sum(u.get('rewrite', {}).get('status') == 'done' for u in uses), 'approved_mothers_v2': sum(mother_approved(data, u) for u in uses), 'legacy_feedback': [u['feedback'] for u in uses if not u.get('managed_v2') and u.get('feedback', {}).get('status') != 'unknown'], 'feedback': [f for f in data['feedback_records'].values() if (f['target'] == 'template' and f['target_id'] == template['id']) or (f['target'] == 'draft' and data['drafts'][f['target_id']]['use_id'] in {u['id'] for u in uses})]})
    return {'exclude_source_ids': sorted(excluded), 'templates': templates}


def render(data):
    lines = ['# 小红书笔记共用台账', '', '生成、用户认可、发布分别记录；裂变不增加模板使用次数。', '', ' · '.join(f'{k}: {v}' for k, v in counts(data).items()), '', '## 任务进度', '']
    for tid in data['tasks']:
        lines.append('- ' + tid + '：' + encoded(progress(data, tid)))
    lines += ['', '## 使用与稿件', '', '| 使用编号 | 来源 | 产品 | 母稿 | 裂变 | 最新母稿反馈 |', '| --- | --- | --- | --- | ---: | --- |']
    def cell(x):
        return str(x).replace('|', '／').replace('\n', ' ')
    for u in data['uses'].values():
        s = data['sources'].get(u.get('source_id'), {})
        d = data['drafts'].get(u['id'] + '--mother')
        feedback = latest_feedback(data, 'draft', d['id'], d['latest'])['status'] if d else '历史记录见原始数据'
        source = '[' + cell(s.get('title', '原创')) + '](' + s.get('url', '') + ')' if s else '原创'
        lines.append('| ' + ' | '.join(map(cell, [u['id'], source, data['products'][u['product_id']]['name'], '已撤回选择' if u.get('status') == 'withdrawn' else u.get('rewrite', {}).get('status'), u.get('variants', {}).get('count', 0), feedback])) + ' |')
    lines += ['', '## 模板复查与使用', '', '| 模板 | 母稿使用次数 | 当前母稿明确认可 | 历史认可记录 | 最近观察 | 复查结论 |', '| --- | ---: | ---: | ---: | --- | --- |']
    for t in query(data)['templates']:
        legacy = sum(f.get('status') == 'approved' for f in t['legacy_feedback'])
        lines.append('| ' + ' | '.join(map(cell, [t['name'], t['mother_uses'], t['approved_mothers_v2'], legacy, t.get('last_observed_at', '未知'), t.get('review_status', '新候选')])) + ' |')
    lines += ['', '旧记录认可范围按原数据保留；裂变认可不自动算母稿认可，用户认可也不代表成交。', '', '## 稿件版本与反馈证据', '']
    for d in data['drafts'].values():
        for rev, v in d['versions'].items():
            lines.append(f"- {d['id']} r{rev} [{cell(v['title'])}]({v['artifact']})；反馈：{latest_feedback(data, 'draft', d['id'], int(rev))['status']}；发布：{v.get('publication', {}).get('status', 'unknown')}")
    for f in data['feedback_records'].values():
        lines.append(f"- 反馈 {f['target_id']} r{f.get('revision', '-')}：{f['status']}；{cell(f['quote'])}；依据：{cell(f['evidence'])}")
    lines += ['', '历史记录保留原聊天位置与原有反馈范围；未补造旧稿或拆分旧裂变篇数。模板观察与完整证据见 ledger.json；下一轮查找使用 query。', '']
    return '\n'.join(lines)


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def materialize(root, data):
    for draft in data['drafts'].values():
        for v in draft['versions'].values():
            path = root / v['artifact']; text = draft_text(v)
            require(digest(text) == v['content_hash'], 'Corrupt stored draft')
            if path.exists():
                require(path.read_text(encoding='utf-8') == text, 'Artifact changed outside ledger; record a new revision')
            else:
                atomic_write(path, text)
    atomic_write(root / '笔记台账.md', render(data))
    for tid in data['tasks']:
        atomic_write(root / 'handoffs' / (tid + '.json'), json.dumps(handoff(data, tid), ensure_ascii=False, indent=2) + '\n')


def read(root):
    path = root / 'ledger.json'
    data = migrate(json.loads(path.read_text(encoding='utf-8'))) if path.exists() else empty()
    validate(data)
    return data


def main():
    # Windows redirected output otherwise inherits a legacy code page. Keep JSON
    # and emoji/Chinese copy intact for callers on every platform.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['show', 'query', 'handoff', 'record', 'migrate', 'render', 'export'])
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('XHS_LEDGER_ROOT', DEFAULT_ROOT)))
    parser.add_argument('--input', type=Path)
    parser.add_argument('--task')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(); root = args.root.expanduser().resolve()
    if args.command in ('show', 'query', 'handoff'):
        # Coordinate reads too: Windows may deny replacing an open JSON file.
        with ledger_lock(root / '.ledger.lock') if root.exists() else nullcontext():
            data = read(root)
        if args.command == 'query':
            out = query(data)
        elif args.command == 'handoff':
            require(args.task, '--task required'); out = handoff(data, args.task)
        else:
            out = {'root': str(root), 'schema_version': VERSION, 'counts': counts(data), 'tasks': {tid: progress(data, tid) for tid in data['tasks']}}
        print(json.dumps(out, ensure_ascii=False, indent=2)); return
    root.mkdir(parents=True, exist_ok=True)
    with ledger_lock(root / '.ledger.lock'):
        path = root / 'ledger.json'; data = read(root); changed = False
        if args.command == 'record':
            require(args.input, '--input required')
            data, changed = reduce(data, json.loads(args.input.read_text(encoding='utf-8-sig')))
        if args.command == 'export':
            require(args.output, '--output must name a NEW directory')
            destination = args.output.expanduser().resolve()
            require(not destination.exists() and not destination.is_relative_to(root), 'Export destination must be new and outside root')
            materialize(root, data)
            destination.mkdir(parents=True)
            atomic_write(destination / 'ledger.json', json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            for name in ('artifacts', 'handoffs', '笔记台账.md', '风格参照.md', '风格参照证据.json'):
                src = root / name
                if src.is_dir(): shutil.copytree(src, destination / name)
                elif src.is_file(): shutil.copy2(src, destination / name)
            print(json.dumps({'export': str(destination), 'counts': counts(data)}, ensure_ascii=False)); return
        # Validate/materialize before commit. A crash may leave unreferenced artifacts, never false ledger counts.
        for d in data['drafts'].values():
            for v in d['versions'].values():
                p = root / v['artifact']
                require(not p.exists() or p.read_text(encoding='utf-8') == draft_text(v), 'Artifact changed outside ledger')
        previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        if changed or previous is None or previous.get('schema_version') != VERSION:
            if path.exists():
                shutil.copy2(path, root / 'ledger.json.bak')
                if previous.get('schema_version') == 1 and not (root / 'ledger.v1.backup.json').exists():
                    shutil.copy2(path, root / 'ledger.v1.backup.json')
            # Artifacts first, canonical ledger next, derived views last and rebuildable.
            for d in data['drafts'].values():
                for v in d['versions'].values():
                    if not (root / v['artifact']).exists(): atomic_write(root / v['artifact'], draft_text(v))
            atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        materialize(root, data)
        print(json.dumps({'root': str(root), 'changed': changed, 'counts': counts(data), 'task': progress(data, args.task) if args.task else None}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise SystemExit('Ledger error: ' + str(error))

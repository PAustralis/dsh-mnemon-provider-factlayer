# -*- coding: utf-8 -*-
"""Parse question bank, map golden memories back to corpus records, dump diagnostics."""
import json, re, unicodedata, sys, io
from collections import Counter, defaultdict

QBANK = r'C:\Users\ZhaoY\Desktop\读者测试40-题库.md'
CORPUS = r'E:\Tools\projects\dsh-mnemon-provider-graphiti\research\phase2\corpus.json'
OUT = r'E:\dsh-mnemon-provider-graphiti\research\recall\diag_mapping.md'


def norm(s):
    s = unicodedata.normalize('NFKC', s)
    # unify fullwidth/quote variants to avoid cosmetic mismatch
    s = s.replace('\u3000', ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def parse_qbank(path):
    raw = open(path, encoding='utf-8').read()
    raw = raw.replace('\r\n', '\n')
    blocks = re.split(r'^## 场景\s*(\d+)\s*$', raw, flags=re.M)
    scenarios = []
    # blocks: [pre, id1, body1, id2, body2, ...]
    for i in range(1, len(blocks), 2):
        sid = int(blocks[i])
        body = blocks[i + 1]
        qm = re.search(r'\*\*用户问\*\*\s*[：:]\s*(.+)', body)
        q = qm.group(1).strip() if qm else ''
        code = re.search(r'```\s*\n(.*?)```', body, flags=re.S)
        mems = []
        if code:
            for line in code.group(1).split('\n'):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'^\[([^\]]+)\]\s*(.*)$', line)
                if m:
                    mems.append({'date_label': m.group(1).strip(), 'text': m.group(2).strip()})
                else:
                    mems.append({'date_label': None, 'text': line, 'no_bracket': True})
        scenarios.append({'sid': sid, 'question': q, 'mems': mems})
    return scenarios


def main():
    corpus = json.load(open(CORPUS, encoding='utf-8'))
    for i, r in enumerate(corpus):
        r['_i'] = i
    by_norm = {}
    for r in corpus:
        by_norm.setdefault(norm(r['text']), []).append(r)

    scen = parse_qbank(QBANK)
    lines = []
    lines.append('# 题库 ↔ 语料 映射诊断')
    lines.append('')
    lines.append('- 题库场景数: %d' % len(scen))
    lines.append('- 语料条数: %d' % len(corpus))

    total_mem = 0
    matched = 0
    unmatched = []
    date_mismatch = []
    scen_mismatch = []
    per_scen = []
    golden_ids = set()
    dup_within = []
    for s in scen:
        ids = []
        seen = set()
        for m in s['mems']:
            total_mem += 1
            key = norm(m['text'])
            if key in by_norm:
                cands = by_norm[key]
                # prefer corpus record whose scenario equals qbank sid
                pick = next((c for c in cands if c.get('scenario') == s['sid']), cands[0])
                ids.append(pick['_i'])
                golden_ids.add(pick['_i'])
                matched += 1
                if len(cands) > 1:
                    dup_within.append((s['sid'], m['text'], [c['_i'] for c in cands]))
                # date label vs raw_date
                rl = m.get('date_label')
                if rl and rl != pick['raw_date']:
                    date_mismatch.append((s['sid'], rl, pick['raw_date'], m['text'][:40]))
                if pick.get('scenario') != s['sid']:
                    scen_mismatch.append((s['sid'], pick['scenario'], m['text'][:40]))
                if pick['_i'] in seen:
                    dup_within.append((s['sid'], 'DUPLICATE_GOLD', m['text'][:40]))
                seen.add(pick['_i'])
            else:
                unmatched.append((s['sid'], m['text']))
        per_scen.append((s['sid'], s['question'], sorted(set(ids))))

    lines.append('- 题库记忆条目总数: %d' % total_mem)
    lines.append('- 精确匹配(NFKC+trim)成功: %d (%.1f%%)' % (matched, 100.0 * matched / max(1, total_mem)))
    lines.append('- 去重后 golden 记录数: %d' % len(golden_ids))
    lines.append('')
    lines.append('## 匹配不上的记忆 (%d 条)' % len(unmatched))
    lines.append('')
    if unmatched:
        lines.append('| 场景 | 记忆原文 |')
        lines.append('|---|---|')
        for sid, t in unmatched:
            lines.append('| %d | %s |' % (sid, t.replace('|', '\\|')))
    else:
        lines.append('无。全部题库记忆都能精确映射回语料。')
    lines.append('')
    lines.append('## 日期标签与 corpus.raw_date 不一致 (%d 处)' % len(date_mismatch))
    lines.append('')
    if date_mismatch:
        lines.append('| 场景 | 题库标签 | corpus.raw_date | 记忆片段 |')
        lines.append('|---|---|---|---|')
        for sid, a, b, t in date_mismatch:
            lines.append('| %d | %s | %s | %s |' % (sid, a, b, t.replace('|', '\\|')))
    else:
        lines.append('无。')
    lines.append('')
    lines.append('## corpus.scenario 与题库场景号不一致 (%d 处)' % len(scen_mismatch))
    lines.append('')
    if scen_mismatch:
        lines.append('| 题库场景 | corpus.scenario | 记忆片段 |')
        lines.append('|---|---|---|')
        for sid, a, t in scen_mismatch:
            lines.append('| %d | %s | %s |' % (sid, a, t.replace('|', '\\|')))
    else:
        lines.append('全部一致（说明题库与语料来自同一份人造场景序列）。')
    lines.append('')
    lines.append('## 每场景 golden 大小')
    lines.append('')
    size = Counter(len(x[2]) for x in per_scen)
    lines.append('| golden 条数 | 场景数 |')
    lines.append('|---|---|')
    for k in sorted(size):
        lines.append('| %d | %d |' % (k, size[k]))
    lines.append('')
    lines.append('## 未被任何题目引用的语料记录')
    lines.append('')
    unused = [r['_i'] for r in corpus if r['_i'] not in golden_ids]
    lines.append('- 数量: %d / %d' % (len(unused), len(corpus)))
    lines.append('')
    open(OUT, 'w', encoding='utf-8').write('\n'.join(lines))

    json.dump({'scenarios': per_scen, 'unmatched': unmatched}, open(
        r'E:\dsh-mnemon-provider-graphiti\research\recall\_mapping.json', 'w', encoding='utf-8'),
        ensure_ascii=False, indent=1)
    print('scenarios', len(scen), 'mem', total_mem, 'matched', matched, 'golden_uniq', len(golden_ids))
    for sid, q, ids in per_scen:
        print(sid, len(ids))


main()

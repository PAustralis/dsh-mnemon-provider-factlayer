# -*- coding: utf-8 -*-
"""把 results.json 渲染进 REPORT.template.md 的表格占位符，产出 REPORT.md。

用法：python make_report.py
改表格结构/新增表格改本文件；改叙述改 REPORT.template.md。
"""
import json, os, itertools, random as _random

BASE = os.path.dirname(os.path.abspath(__file__))
res = json.load(open(os.path.join(BASE, 'results.json'), encoding='utf-8'))
runs = res['runs']
ROUTES = ['R1', 'R2', 'R3', 'R4', 'R5']
NAME = {'R1': 'R1 向量(替身)', 'R2': 'R2 trigram', 'R3': 'R3 二元组',
        'R4': 'R4 最近50', 'R5': 'R5 unicode61'}
mem_n = {x['sid']: x['mem_n'] for x in res['per_q']}
BIG = sorted(s for s, n in mem_n.items() if n >= 6)
SMALL = sorted(s for s, n in mem_n.items() if n < 6)


def pick(tag, subset, **kw):
    for r in runs:
        if r['tag'] != tag or r['subset'] != subset:
            continue
        if all(r.get(k) == v for k, v in kw.items()):
            return r
    return None


def f4(x):
    return '%.4f' % x


def f2(x):
    return '%.2f' % x


def paired(a_s, b_s):
    a = {p['sid']: p['r6'] for p in pick('abl', a_s, mmr=False)['per']}
    b = {p['sid']: p['r6'] for p in pick('abl', b_s, mmr=False)['per']}
    sids = sorted(a)
    d = [a[s] - b[s] for s in sids]
    rng = _random.Random(7)
    boots = []
    for _ in range(2000):
        idx = [rng.randrange(len(d)) for _ in d]
        boots.append(sum(d[i] for i in idx) / len(d))
    boots.sort()
    lo, hi = boots[int(0.025 * len(boots))], boots[int(0.975 * len(boots)) - 1]
    win = sum(1 for x in d if x > 1e-9)
    loss = sum(1 for x in d if x < -1e-9)
    return sum(d) / len(d), lo, hi, win, len(d) - win - loss, loss


def split_of(rec):
    per = {p['sid']: p for p in rec['per']}
    return (sum(per[s]['r6'] for s in SMALL) / len(SMALL),
            sum(per[s]['r6'] for s in BIG) / len(BIG),
            sum(per[s]['hits6'] for s in SMALL) / len(SMALL))


T = {}

# ---- G1 数据概览
L = ['| 项 | 值 |', '|---|---|']
L.append('| 语料 | `E:\\Tools\\projects\\dsh-mnemon-provider-graphiti\\research\\phase2\\corpus.json`，%d 条 |' % res['n_docs'])
L.append('| 题库 | `读者测试40-题库.md`，%d 个场景 |' % res['n_queries'])
L.append('| golden 集 | 题库每个场景「检索到的记忆」代码块内的记录（按 text 精确匹配回语料） |')
L.append('| golden 大小分布 | 2 条×16 题 / 3 条×17 题 / 4 条×3 题 / 6 条×4 题 |')
L.append('| 映射成功率 | 119/119 = 100%（NFKC + 去首尾空白后精确匹配） |')
L.append('| 随机排序基线 | recall@6 = %s，recall@10 = %s，MRR = %s |' % (
    f4(res['random_baseline']['r6']), f4(res['random_baseline']['r10']),
    f4(res['random_baseline']['mrr'])))
L.append('| oracle 上界 | recall@6 = %s（所有 golden 集都 ≤ 6 条，理想检索可全中） |' % f4(res['oracle_recall6']))
T['G1'] = L

# ---- G2 单路
L = ['| 路由 | recall@6 | recall@10 | MRR | 平均命中数@6 | 候选池覆盖@50 | 空结果题数 | 平均返回条数 |',
     '|---|---|---|---|---|---|---|---|']
for r in sorted(ROUTES, key=lambda r: -pick('abl', r)['recall6']):
    a, d = pick('abl', r), res['route_diag'][r]
    L.append('| %s | %s | %s | %s | %s | %s | %d | %.1f |' % (
        NAME[r], f4(a['recall6']), f4(a['recall10']), f4(a['mrr']), f2(a['hits6']),
        f4(d['recall50']), d['empty'], d['avg_len']))
T['G2'] = L

# ---- G3 全部组合
L = ['| 组合 | 路数 | recall@6 | recall@10 | MRR | 命中@6 | +MMR 后 recall@6 | ΔMMR |',
     '|---|---|---|---|---|---|---|---|']
rows = []
for sz in range(1, 6):
    for c in itertools.combinations(ROUTES, sz):
        s = '+'.join(c)
        rows.append((sz, s, pick('abl', s, mmr=False), pick('abl', s, mmr=True)))
rows.sort(key=lambda x: (x[0], -x[2]['recall6']))
for sz, s, a, b in rows:
    L.append('| %s | %d | %s | %s | %s | %s | %s | %+.4f |' % (
        s, sz, f4(a['recall6']), f4(a['recall10']), f4(a['mrr']), f2(a['hits6']),
        f4(b['recall6']), b['recall6'] - a['recall6']))
T['G3'] = L

# ---- G4 增量归因
L = ['| 拿掉谁 / 剩下 | recall@6 | Δ vs 全四路 |', '|---|---|---|']
full = pick('abl', 'R1+R2+R3+R4', mmr=False)
for r in ROUTES[:4]:
    sub = '+'.join([x for x in ROUTES[:4] if x != r])
    a = pick('abl', sub, mmr=False)
    L.append('| 拿掉 %s（剩 %s） | %s | %+.4f |' % (NAME[r], sub, f4(a['recall6']),
                                                  a['recall6'] - full['recall6']))
a = pick('abl', 'R1+R2+R3', mmr=False)
L.append('| —（全四路） | %s | — |' % f4(full['recall6']))
L.append('| 只有 R1+R2+R3（对比：一开始就不要 R4） | %s | %+.4f |' % (
    f4(a['recall6']), a['recall6'] - full['recall6']))
T['G4'] = L

# ---- G5 截断
L = ['| 组合 | T=10 | T=20 | T=50 |', '|---|---|---|---|']
for s in ['R1', 'R3', 'R2+R3', 'R1+R3', 'R1+R2+R3', 'R1+R2+R3+R4', 'R1+R2+R3+R4+R5']:
    vals = res['trunc_sens'].get(s, {})
    cells = [f4(vals[str(t)]) if str(t) in vals else '—' for t in (10, 20, 50)]
    L.append('| %s | %s | %s | %s |' % (s, cells[0], cells[1], cells[2]))
T['G5'] = L

# ---- G6 k
L = ['| 组合 | k=10 | k=30 | k=60 | k=100 |', '|---|---|---|---|---|']
for s, d in res['k_sens'].items():
    L.append('| %s | %s | %s | %s | %s |' % (s, f4(d['10']), f4(d['30']), f4(d['60']), f4(d['100'])))
T['G6'] = L

# ---- G7 bootstrap
L = ['| 对比 | Δrecall@6 | 95% CI | 赢/平/输（题数） | 显著 |', '|---|---|---|---|---|']
for a_s, b_s in [('R3', 'R1'), ('R1+R3', 'R3'), ('R2+R3', 'R3'), ('R1+R2+R3', 'R3'),
                 ('R1+R2+R3+R4', 'R3'), ('R1+R2+R3+R4+R5', 'R3'),
                 ('R1+R4', 'R1'), ('R3+R4', 'R3'), ('R1+R2+R3+R4', 'R1+R2+R3'),
                 ('R1', 'R4')]:
    m, lo, hi, wn, tie, ls = paired(a_s, b_s)
    L.append('| %s − %s | %+.4f | [%+.4f, %+.4f] | %d/%d/%d | %s |' % (
        a_s, b_s, m, lo, hi, wn, tie, ls, '是' if (lo > 0 or hi < 0) else '否'))
T['G7'] = L

# ---- G8 lambda
L = ['| 组合 | λ=0.3 | λ=0.5 | λ=0.7 | λ=0.9 | 不加 MMR |', '|---|---|---|---|---|---|']
for s, d in res['lam_sens'].items():
    L.append('| %s | %s | %s | %s | %s | %s |' % (
        s, f4(d['0.3']), f4(d['0.5']), f4(d['0.7']), f4(d['0.9']),
        f4(pick('abl', s, mmr=False)['recall6'])))
T['G8'] = L

# ---- G9 互补性
L = ['| 关系 | 条数 |', '|---|---|']
for a, b in [('R1', 'R3'), ('R1', 'R2'), ('R3', 'R2'), ('R1', 'R4'), ('R3', 'R4')]:
    L.append('| 只有 %s 命中 / %s 未命中 | %d |' % (a, b, res['only_hits']['%s\\%s' % (a, b)]))
    L.append('| 只有 %s 命中 / %s 未命中 | %d |' % (b, a, res['only_hits']['%s\\%s' % (b, a)]))
T['G9'] = L

# ---- G11 并集覆盖率句子
T['G11'] = ['R1 与 R3 的 top-50 并集覆盖率 %s：与 R1 单独的 %s 完全相同——**R3 没有捞到任何 R1 漏掉的记录**，'
            '它只是把其中一部分排得更靠前。' % (f4(res['union13_recall50']),
                                f4(res['route_diag']['R1']['recall50']))]

# ---- G10 按 golden 性质拆分
L = ['| 组合 | 全 40 题 R@6 | 36 题子集 R@6 | 4 个干扰场景 R@6 | 36 题子集 命中@6 |',
     '|---|---|---|---|---|']
for s in ['R1', 'R2', 'R3', 'R4', 'R5', 'R1+R3', 'R2+R3', 'R1+R2+R3', 'R1+R2+R3+R4',
          'R1+R2+R3+R4+R5']:
    a = pick('abl', s, mmr=False)
    x, y, h = split_of(a)
    L.append('| %s | %s | %s | %s | %s |' % (s, f4(a['recall6']), f4(x), f4(y), f2(h)))
for s in ['R1+R3', 'R1+R2+R3+R4']:
    a = pick('abl', s, mmr=True)
    x, y, h = split_of(a)
    L.append('| %s +MMR | %s | %s | %s | %s |' % (s, f4(a['recall6']), f4(x), f4(y), f2(h)))
T['G10'] = L

# ---- 渲染
tpl = open(os.path.join(BASE, 'REPORT.template.md'), encoding='utf-8').read()
missing = [k for k in T if ('{{%s}}' % k) not in tpl]
if missing:
    print('!! 模板里缺少占位符:', missing)
for k in sorted(T, key=lambda x: -int(x[1:])):
    tpl = tpl.replace('{{%s}}' % k, '\n'.join(T[k]))
left = [x for x in ('{{%s}' % 'G') if False]
if '{{' in tpl:
    print('!! 仍有未替换的占位符')
open(os.path.join(BASE, 'REPORT.md'), 'w', encoding='utf-8').write(tpl)
print('REPORT.md rendered (%d bytes)' % len(tpl))

# 一致性自检：模板里的 %d 之类不该残留
if '%%' in tpl:
    print('!! 有未格式化的 %%')

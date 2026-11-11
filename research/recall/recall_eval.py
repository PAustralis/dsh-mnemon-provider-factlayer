# -*- coding: utf-8 -*-
"""四路召回评测原型（一次性，非产品代码）。见 README.md 的 run 说明。"""
import json, os, re, sqlite3, sys, unicodedata, math, itertools, random
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
CORPUS = r'E:\Tools\projects\dsh-mnemon-provider-graphiti\research\phase2\corpus.json'
QBANK = r'C:\Users\ZhaoY\Desktop\读者测试40-题库.md'
DBPATH = os.path.join(BASE, '_tmp_eval.sqlite')

TOP_K_ROUTE = 50          # 每路取 top-50
FINAL_K = 6               # 输出 top-6
MMR_LAMBDA = 0.7
MMR_POOL = 20             # MMR 在融合结果前 20 名里选
RRF_K = 60

# ---------------------------------------------------------------- text utils
CJK = r'\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff'
RE_SEG = re.compile('[%s]+|[A-Za-z0-9]+(?:[.\\-_][A-Za-z0-9]+)*' % CJK)
RE_CJK_RUN = re.compile('[%s]+' % CJK)


def norm(s):
    s = unicodedata.normalize('NFKC', s)
    s = s.replace('\u3000', ' ')
    return re.sub(r'\s+', ' ', s).strip()


def segments(s):
    out = []
    for m in RE_SEG.finditer(s):
        seg = m.group(0)
        out.append(('cjk' if RE_CJK_RUN.fullmatch(seg) else 'latin', seg))
    return out


def bigrams_of(s):
    toks = []
    for kind, seg in segments(s):
        if kind == 'cjk':
            if len(seg) == 1:
                toks.append(seg)
            else:
                toks.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        else:
            toks.append(seg.lower())
    return toks


def char_ngrams(s, ns=(2, 3)):
    t = re.sub(r'\s+', '', s)
    grams = []
    for n in ns:
        grams.extend(t[i:i + n] for i in range(max(0, len(t) - n + 1)))
    return grams


def parse_date(s):
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', s or '')
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r'^(\d{4})-(\d{2})$', s or '')
    if m:
        return (int(m.group(1)), int(m.group(2)), 0)
    m = re.match(r'^(\d{4})$', s or '')
    if m:
        return (int(m.group(1)), 0, 0)
    return (-1, 0, 0)          # 日期不详 -> 视为最旧


# ---------------------------------------------------------------- 题库解析
def parse_qbank(path):
    raw = open(path, encoding='utf-8').read().replace('\r\n', '\n')
    blocks = re.split(r'^## 场景\s*(\d+)\s*$', raw, flags=re.M)
    scen = []
    for i in range(1, len(blocks), 2):
        sid, body = int(blocks[i]), blocks[i + 1]
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
                mems.append({'date_label': m.group(1).strip() if m else None,
                             'text': (m.group(2) if m else line).strip()})
        scen.append({'sid': sid, 'question': q, 'mems': mems})
    return scen


# ---------------------------------------------------------------- 索引
def build_db(corpus):
    if os.path.exists(DBPATH):
        os.remove(DBPATH)
    con = sqlite3.connect(DBPATH)
    con.execute('PRAGMA journal_mode=OFF')
    con.execute('''CREATE TABLE docs(docid INTEGER PRIMARY KEY, scenario INT, cls TEXT,
                   raw_date TEXT, dkey TEXT, text TEXT, bg TEXT)''')
    con.execute("CREATE VIRTUAL TABLE fts_tri USING fts5(text, tokenize='trigram')")
    con.execute("CREATE VIRTUAL TABLE fts_bi  USING fts5(bg,   tokenize='unicode61')")
    con.execute("CREATE VIRTUAL TABLE fts_uni USING fts5(text, tokenize='unicode61')")
    for i, r in enumerate(corpus):
        t = norm(r['text'])
        bg = ' '.join(bigrams_of(t))
        con.execute('INSERT INTO docs VALUES (?,?,?,?,?,?,?)',
                    (i, r.get('scenario'), r.get('cls'), r.get('raw_date'),
                     json.dumps(parse_date(r.get('raw_date'))), t, bg))
        con.execute('INSERT INTO fts_tri(rowid, text) VALUES (?,?)', (i, t))
        con.execute('INSERT INTO fts_bi(rowid, bg) VALUES (?,?)', (i, bg))
        con.execute('INSERT INTO fts_uni(rowid, text) VALUES (?,?)', (i, t))
    con.commit()
    return con


# ---------------------------------------------------------------- 查询构造
def q_trigram(q):
    terms = []
    for kind, seg in segments(q):
        if kind == 'cjk':
            terms.extend(seg[i:i + 3] for i in range(len(seg) - 2))
        else:
            terms.append(seg.lower())
    return terms


def q_bigram(q):
    return bigrams_of(q)


def q_unicode(q):
    return [seg for _k, seg in segments(q)]


def fts_match_expr(terms):
    seen, uniq = set(), []
    for t in terms:
        t = t.replace('"', '')
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return ' OR '.join('"%s"' % t for t in uniq) if uniq else None


# ---------------------------------------------------------------- 向量（替身）
class TfidfSpace:
    """字符 2-3gram TF-IDF 余弦：本地无 embedding 模型时的向量路替身。"""

    def __init__(self, docs):
        import numpy as np
        grams = [char_ngrams(d) for d in docs]
        df = Counter()
        for g in grams:
            for k in set(g):
                df[k] += 1
        self.vocab = {}
        for g in grams:
            for k in g:
                self.vocab.setdefault(k, len(self.vocab))
        N = len(docs)
        self.idf = {k: math.log((1.0 + N) / (1.0 + df[k])) + 1.0 for k in df}
        M = np.zeros((N, len(self.vocab)), dtype='float32')
        for i, g in enumerate(grams):
            for k, c in Counter(g).items():
                M[i, self.vocab[k]] = (1.0 + math.log(c)) * self.idf[k]
        nrm = np.linalg.norm(M, axis=1, keepdims=True)
        nrm[nrm == 0] = 1.0
        self.M = M / nrm
        self.dim = len(self.vocab)

    def encode(self, text):
        import numpy as np
        v = np.zeros(self.dim, dtype='float32')
        for k, c in Counter(char_ngrams(text)).items():
            j = self.vocab.get(k)
            if j is not None:
                v[j] = (1.0 + math.log(c)) * self.idf[k]
        n = np.linalg.norm(v)
        return v / n if n > 0 else v


def rank_fts(con, table, expr, limit=TOP_K_ROUTE):
    if not expr:
        return []
    sql = ('SELECT rowid FROM %s WHERE %s MATCH ? ORDER BY bm25(%s) ASC, rowid ASC LIMIT ?'
           % (table, table, table))
    try:
        return [r[0] for r in con.execute(sql, (expr, limit)).fetchall()]
    except sqlite3.OperationalError as e:
        print('  ! FTS error', table, e)
        return []


# ---------------------------------------------------------------- 指标
def metrics(ranking, gold, final_k=FINAL_K):
    gold = set(gold)
    g = max(1, len(gold))
    r6 = len(set(ranking[:final_k]) & gold) / g
    r10 = len(set(ranking[:10]) & gold) / g
    mrr = 0.0
    for rk, d in enumerate(ranking, 1):
        if d in gold:
            mrr = 1.0 / rk
            break
    return {'r6': r6, 'r10': r10, 'mrr': mrr, 'hits6': len(set(ranking[:final_k]) & gold)}


def main():
    import numpy as np
    corpus = json.load(open(CORPUS, encoding='utf-8'))
    texts = [norm(r['text']) for r in corpus]
    N = len(corpus)
    scen = parse_qbank(QBANK)

    by_norm = defaultdict(list)
    for i, t in enumerate(texts):
        by_norm[t].append(i)
    queries, unmatched = [], []
    for s in scen:
        ids = set()
        for m in s['mems']:
            cands = by_norm.get(norm(m['text']))
            if not cands:
                unmatched.append((s['sid'], m['text']))
                continue
            ids.add(min(cands, key=lambda i: (corpus[i].get('scenario') != s['sid'], i)))
        queries.append({'sid': s['sid'], 'q': norm(s['question']), 'gold': ids,
                        'mem_n': len(s['mems'])})

    con = build_db(corpus)
    space = TfidfSpace(texts)
    order = sorted(range(N), key=lambda i: (parse_date(corpus[i].get('raw_date')), i), reverse=True)
    r4_all = order[:TOP_K_ROUTE]

    per_q = []
    for qq in queries:
        q = qq['q']
        v = space.encode(q)
        sims = space.M @ v
        per_q.append({
            'sid': qq['sid'], 'q': q, 'gold': qq['gold'], 'mem_n': qq['mem_n'],
            'routes': {
                'R1': [int(i) for i in np.argsort(-sims, kind='stable')[:TOP_K_ROUTE]],
                'R2': rank_fts(con, 'fts_tri', fts_match_expr(q_trigram(q))),
                'R3': rank_fts(con, 'fts_bi', fts_match_expr(q_bigram(q))),
                'R4': r4_all,
                'R5': rank_fts(con, 'fts_uni', fts_match_expr(q_unicode(q))),
            }})

    def rrf(lists, k):
        sc, best = defaultdict(float), {}
        for _name, lst in lists:
            for rk, d in enumerate(lst, 1):
                sc[d] += 1.0 / (k + rk)
                if d not in best or rk < best[d]:
                    best[d] = rk
        return sorted(sc.items(), key=lambda kv: (-kv[1], best[kv[0]], kv[0]))

    def mmr_rank(fused, sel_n, lam):
        pool = [d for d, _ in fused][:MMR_POOL]
        if not pool:
            return []
        vals = [s for _d, s in fused][:len(pool)]
        lo, hi = min(vals), max(vals)
        rel = {d: ((s - lo) / (hi - lo) if hi > lo else 1.0 / (i + 1))
               for i, (d, s) in enumerate(fused[:len(pool)])}
        sel, rest = [], list(pool)
        while rest and len(sel) < sel_n:
            best, bestv = None, None
            for d in rest:
                div = max([float(space.M[d] @ space.M[s]) for s in sel], default=0.0)
                val = lam * rel[d] - (1 - lam) * div
                if bestv is None or val > bestv + 1e-12:
                    best, bestv = d, val
            sel.append(best)
            rest.remove(best)
        return sel

    runs = []

    def run(tag, subset, k=RRF_K, trunc=TOP_K_ROUTE, use_mmr=False, lam=MMR_LAMBDA):
        per = []
        for item in per_q:
            fused = rrf([(r, item['routes'][r][:trunc]) for r in subset], k)
            ranking = [d for d, _ in fused]
            if use_mmr:
                ranking = mmr_rank(fused, max(FINAL_K, 10), lam)
            m = metrics(ranking, item['gold'])
            m['sid'] = item['sid']
            per.append(m)
        rec = {'tag': tag, 'subset': '+'.join(subset), 'k': k, 'trunc': trunc,
               'mmr': bool(use_mmr), 'lam': lam if use_mmr else None,
               'recall6': float(np.mean([p['r6'] for p in per])),
               'recall10': float(np.mean([p['r10'] for p in per])),
               'mrr': float(np.mean([p['mrr'] for p in per])),
               'hits6': float(np.mean([p['hits6'] for p in per])),
               'per': [{'sid': p['sid'], 'r6': p['r6'], 'r10': p['r10'],
                        'mrr': p['mrr'], 'hits6': p['hits6']} for p in per]}
        runs.append(rec)
        return rec

    ROUTES = ['R1', 'R2', 'R3', 'R4', 'R5']
    subsets = [c for sz in range(1, 6) for c in itertools.combinations(ROUTES, sz)]

    # 主实验：所有子集 × {不加, 加 MMR}
    for s in subsets:
        run('abl', s, use_mmr=False)
        run('abl', s, use_mmr=True)

    # 截断敏感：每路只取前 T 名参与 RRF
    for T in (10, 20, 50):
        for s in [('R1',), ('R3',), ('R4',), ('R1', 'R3'), ('R2', 'R3'),
                  ('R1', 'R2', 'R3'), ('R1', 'R2', 'R3', 'R4')]:
            run('trunc', s, trunc=T)
    run('trunc', ('R1', 'R2', 'R3', 'R4', 'R5'), trunc=10)
    run('trunc', ('R1', 'R2', 'R3', 'R4', 'R5'), trunc=20)

    # RRF k 敏感
    for k in (10, 30, 60, 100):
        for s in [('R2', 'R3'), ('R1', 'R3'), ('R1', 'R2', 'R3'),
                  ('R1', 'R2', 'R3', 'R4'), ('R1', 'R2', 'R3', 'R4', 'R5')]:
            run('ksens', s, k=k)

    # MMR lambda 敏感
    for lam in (0.3, 0.5, 0.7, 0.9):
        for s in [('R1', 'R3'), ('R1', 'R2', 'R3', 'R4')]:
            run('lam', s, use_mmr=True, lam=lam)

    # 单路诊断：候选池覆盖率 / 空结果 / 平均长度 / 只在单路命中的 golden 数
    route_diag, only = {}, {}
    for r in ROUTES:
        cover, empty, lens = [], 0, []
        for item in per_q:
            lst = item['routes'][r]
            lens.append(len(lst))
            empty += (not lst)
            cover.append(len(set(lst) & item['gold']) / max(1, len(item['gold'])))
        route_diag[r] = {'recall50': float(np.mean(cover)), 'empty': int(empty),
                         'avg_len': float(np.mean(lens))}

    gold_all = [(item, set(item['gold'])) for item in per_q]
    for a, b in itertools.combinations(ROUTES, 2):
        only['%s_only' % a] = only.get('%s_only' % a, 0) + 0  # placeholder
    only_hits = defaultdict(int)
    for a, b in itertools.combinations(ROUTES, 2):
        for item in per_q:
            g = set(item['gold'])
            ha, hb = set(item['routes'][a]) & g, set(item['routes'][b]) & g
            only_hits['%s\\%s' % (a, b)] += len(ha - hb)
            only_hits['%s\\%s' % (b, a)] += len(hb - ha)

    union13 = float(np.mean([len((set(x['routes']['R1']) | set(x['routes']['R3'])) & set(x['gold']))
                             / max(1, len(x['gold'])) for x in per_q]))

    # 随机基线（蒙特卡洛）
    rnd = random.Random(20261111)
    rb = []
    for _t in range(300):
        acc = []
        for item in per_q:
            perm = list(range(N))
            rnd.shuffle(perm)
            acc.append(metrics(perm, item['gold']))
        rb.append([np.mean([a['r6'] for a in acc]), np.mean([a['r10'] for a in acc]),
                   np.mean([a['mrr'] for a in acc])])
    rb = np.array(rb).mean(axis=0)

    out = {
        'n_docs': N, 'n_queries': len(queries), 'unmatched': unmatched,
        'route_diag': route_diag, 'only_hits': dict(only_hits), 'union13_recall50': union13,
        'gold_sizes': [len(x['gold']) for x in queries],
        'oracle_recall6': float(np.mean([min(FINAL_K, len(x['gold'])) / len(x['gold'])
                                        for x in queries])),
        'random_baseline': {'r6': float(rb[0]), 'r10': float(rb[1]), 'mrr': float(rb[2])},
        'runs': runs,
        'k_sens': {}, 'lam_sens': {}, 'trunc_sens': {},
        'per_q': [{'sid': x['sid'], 'q': x['q'], 'gold': sorted(x['gold']),
                   'routes': {k: v for k, v in x['routes'].items()}, 'mem_n': x['mem_n']}
                  for x in per_q],
    }
    for rec in runs:
        if rec['tag'] == 'ksens':
            out['k_sens'].setdefault(rec['subset'], {})[rec['k']] = rec['recall6']
        elif rec['tag'] == 'lam':
            out['lam_sens'].setdefault(rec['subset'], {})[rec['lam']] = rec['recall6']
        elif rec['tag'] == 'trunc':
            out['trunc_sens'].setdefault(rec['subset'], {})[rec['trunc']] = rec['recall6']

    json.dump(out, open(os.path.join(BASE, 'results.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)

    print('docs=%d queries=%d unmatched=%d' % (N, len(queries), len(unmatched)))
    print('oracle R@6=%.4f  random R@6=%.4f R@10=%.4f MRR=%.4f' %
          (out['oracle_recall6'], rb[0], rb[1], rb[2]))
    for r in ROUTES:
        d = route_diag[r]
        print('  %s pool_cov@50=%.3f empty=%d avg_len=%.1f' %
              (r, d['recall50'], d['empty'], d['avg_len']))
    print('  R1|R3 union cov@50 = %.3f' % union13)
    for rec in runs:
        if rec['tag'] == 'abl' and not rec['mmr'] and len(rec['subset'].split('+')) <= 4:
            pass
    con.close()


if __name__ == '__main__':
    main()

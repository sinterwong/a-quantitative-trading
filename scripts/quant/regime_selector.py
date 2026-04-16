import os, sys

SCRIPTS_DIR = r'C:\Users\sinte\.openclaw\workspace\quant_repo\scripts'
SRC = os.path.join(SCRIPTS_DIR, 'dynamic_selector.py')

with open(SRC, encoding='utf-8') as f:
    content = f.read()

# ── 1. Patch __init__ signature ──────────────────────────────────────
OLD_SIG = '    def __init__(self):'
NEW_SIG = "    def __init__(self, regime: str = 'CALM'):"

if OLD_SIG in content:
    content = content.replace(OLD_SIG, NEW_SIG, 1)
    print('1. Patched __init__ signature OK')
else:
    print('ERROR: __init__ signature not found')
    sys.exit(1)

# ── 2. Add self.regime after last_news_source ─────────────────────────
# Strategy: find the line "self._last_news_source...not_tried'\n\n"
# and insert self.regime before the \n\n (i.e., keep one \n, add regime line, keep the other \n)
marker = "        self._last_news_source: str = 'not_tried'\n\n    # -"
if marker in content:
    new_marker = (
        "        self._last_news_source: str = 'not_tried'\n"
        "        self.regime: str = regime  # BULL / BEAR / VOLATILE / CALM\n\n"
        "    # "
    )
    content = content.replace(marker, new_marker, 1)
    print('2. Added self.regime attribute OK')
else:
    print('ERROR: marker not found')
    idx = content.find("self._last_news_source")
    if idx >= 0:
        snippet = content[idx:idx+100]
        print('Found at', repr(snippet[:80]))
    sys.exit(1)

# ── 3. Patch bk_final build ────────────────────────────────────────────
OLD_BUILD = """            bk_final[bk] = {
                'name': bk_name,
                'total': total,
                'news': news_score,
                'perf': perf,
                'flow': flow,
                'tech': tech,
                'consistency': consistency,
                'sentiment': round(sentiment_bonus, 2),
                'change_pct': info['change_pct'],
                'net_flow': info['net_flow'],
            }"""

NEW_BUILD = """            base_info = {
                'name': bk_name,
                'total': total,
                'news': news_score,
                'perf': perf,
                'flow': flow,
                'tech': tech,
                'consistency': consistency,
                'sentiment': round(sentiment_bonus, 2),
                'change_pct': info['change_pct'],
                'net_flow': info['net_flow'],
            }
            bk_final[bk] = _regime_modulate(base_info, getattr(self, 'regime', 'CALM'))"""

if OLD_BUILD in content:
    content = content.replace(OLD_BUILD, NEW_BUILD)
    print('3. Patched bk_final build OK')
else:
    print('ERROR: bk_final build pattern not found')
    sys.exit(1)

# ── 4. Patch select_stocks ──────────────────────────────────────────────
OLD_SELECT = "    def select_stocks(self, top_n: int = 5) -> List[str]:"
NEW_SELECT = "    def select_stocks(self, top_n: int = 5, regime: str = None) -> List[str]:"

if OLD_SELECT in content:
    content = content.replace(OLD_SELECT, NEW_SELECT)
    print('4. Patched select_stocks OK')
else:
    print('ERROR: select_stocks pattern not found')
    sys.exit(1)

# ── 5. Insert regime constants before __main__ ─────────────────────────
INSERT_MARKER = "if __name__ == '__main__':"
INSERT_BLOCK = '''
# ─── P6.3 环境感知 ───────────────────────────────────────────────────────

DEFENSIVE_SECTORS = {'电力', '医药', '医疗', '消费', '银行', '食品', '家电', '农业'}
MOMENTUM_SECTORS = {'AI', '芯片', '半导体', '5G', '新能源', '军工', '新能源汽车',
                    '人工智能', 'eVTOL', '机器人', '算力', '光模块', '游戏'}

def _regime_modulate(score_dict: dict, regime: str) -> dict:
    """Modulate sector score based on market regime."""
    import copy
    d = copy.copy(score_dict)
    total = d.get('total', 0)
    boost = 0

    if regime == 'BULL':
        for m in MOMENTUM_SECTORS:
            if m in d.get('name', ''):
                total *= 1.2
                boost = 1
                break
    elif regime == 'BEAR':
        defended = any(ds in d.get('name', '') for ds in DEFENSIVE_SECTORS)
        total *= 1.2 if defended else 0.85
        boost = 1 if defended else -1
    elif regime == 'VOLATILE':
        total *= 0.80
        boost = -1

    d['total'] = total
    d['regime_boost'] = boost
    return d


'''

if INSERT_MARKER in content:
    content = content.replace(INSERT_MARKER, INSERT_BLOCK + INSERT_MARKER)
    print('5. Inserted regime constants OK')
else:
    content = content + INSERT_BLOCK
    print('5. Appended regime constants (no __main__)')

# ── Write ────────────────────────────────────────────────────────────────
with open(SRC, 'w', encoding='utf-8') as f:
    f.write(content)
print('Written to dynamic_selector.py')

import ast
try:
    ast.parse(content)
    print('Syntax check: OK')
except SyntaxError as e:
    print(f'Syntax ERROR at line {e.lineno}: {e.msg}')

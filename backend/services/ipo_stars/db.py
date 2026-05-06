"""
db.py — IPO Stars 数据库层
=========================
使用项目统一的 SQLite 数据库（portfolio.db）。
"""

import logging
from datetime import datetime
from typing import List, Optional, Dict, Any

from services.portfolio import get_cursor

logger = logging.getLogger('ipo_stars.db')


# ─── Schema ───────────────────────────────────────────────────

def init_ipo_tables():
    """创建 IPO Stars 所需的表（幂等）。"""
    with get_cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS ipo_candidates (
                code                TEXT PRIMARY KEY,
                name                TEXT NOT NULL DEFAULT '',
                status              TEXT NOT NULL DEFAULT 'upcoming',
                listing_date        TEXT DEFAULT '',
                offer_price_low     REAL DEFAULT 0,
                offer_price_high    REAL DEFAULT 0,
                offer_price_final   REAL DEFAULT 0,
                issue_size          REAL DEFAULT 0,
                sponsor             TEXT DEFAULT '',
                stabilizer          TEXT DEFAULT '',
                cornerstone_names   TEXT DEFAULT '',
                cornerstone_pct     REAL DEFAULT 0,
                public_offer_multiple REAL DEFAULT 0,
                clawback_pct        REAL DEFAULT 0,
                margin_multiple     REAL DEFAULT 0,
                industry            TEXT DEFAULT '',
                pre_ipo_cost        REAL DEFAULT 0,
                created_at          TEXT DEFAULT '',
                updated_at          TEXT DEFAULT ''
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS ipo_analyses (
                code                TEXT PRIMARY KEY,
                final_score         REAL DEFAULT 0,
                recommendation      TEXT DEFAULT '',
                heat_level          TEXT DEFAULT '',
                control_level       TEXT DEFAULT '',
                sentiment_score     REAL DEFAULT 0,
                chips_score         REAL DEFAULT 0,
                narrative_score     REAL DEFAULT 0,
                valuation_score     REAL DEFAULT 0,
                pricing_json        TEXT DEFAULT '{}',
                risk_alerts_json    TEXT DEFAULT '[]',
                key_factors_json    TEXT DEFAULT '[]',
                analyzed_at         TEXT DEFAULT ''
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS ipo_subscriptions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                code        TEXT NOT NULL,
                strategy    TEXT NOT NULL DEFAULT 'neutral',
                subscribed_at TEXT DEFAULT '',
                UNIQUE(code, strategy)
            )
        ''')

    logger.info('IPO tables initialized')


# ─── Candidates CRUD ─────────────────────────────────────────

def upsert_candidate(data: Dict[str, Any]) -> None:
    """插入或更新 IPO 候选标的。"""
    now = datetime.now().isoformat()
    with get_cursor() as cur:
        cur.execute('''
            INSERT INTO ipo_candidates
                (code, name, status, listing_date,
                 offer_price_low, offer_price_high, offer_price_final,
                 issue_size, sponsor, stabilizer,
                 cornerstone_names, cornerstone_pct,
                 public_offer_multiple, clawback_pct, margin_multiple,
                 industry, pre_ipo_cost, created_at, updated_at)
            VALUES
                (:code, :name, :status, :listing_date,
                 :offer_price_low, :offer_price_high, :offer_price_final,
                 :issue_size, :sponsor, :stabilizer,
                 :cornerstone_names, :cornerstone_pct,
                 :public_offer_multiple, :clawback_pct, :margin_multiple,
                 :industry, :pre_ipo_cost, :created_at, :updated_at)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                status = excluded.status,
                listing_date = excluded.listing_date,
                offer_price_low = excluded.offer_price_low,
                offer_price_high = excluded.offer_price_high,
                offer_price_final = excluded.offer_price_final,
                issue_size = excluded.issue_size,
                sponsor = excluded.sponsor,
                stabilizer = excluded.stabilizer,
                cornerstone_names = excluded.cornerstone_names,
                cornerstone_pct = excluded.cornerstone_pct,
                public_offer_multiple = excluded.public_offer_multiple,
                clawback_pct = excluded.clawback_pct,
                margin_multiple = excluded.margin_multiple,
                industry = excluded.industry,
                pre_ipo_cost = excluded.pre_ipo_cost,
                updated_at = excluded.updated_at
        ''', {
            'code': data['code'],
            'name': data.get('name', ''),
            'status': data.get('status', 'upcoming'),
            'listing_date': data.get('listing_date', ''),
            'offer_price_low': float(data.get('offer_price_low', 0)),
            'offer_price_high': float(data.get('offer_price_high', 0)),
            'offer_price_final': float(data.get('offer_price_final', 0)),
            'issue_size': float(data.get('issue_size', 0)),
            'sponsor': data.get('sponsor', ''),
            'stabilizer': data.get('stabilizer', ''),
            'cornerstone_names': data.get('cornerstone_names', ''),
            'cornerstone_pct': float(data.get('cornerstone_pct', 0)),
            'public_offer_multiple': float(data.get('public_offer_multiple', 0)),
            'clawback_pct': float(data.get('clawback_pct', 0)),
            'margin_multiple': float(data.get('margin_multiple', 0)),
            'industry': data.get('industry', ''),
            'pre_ipo_cost': float(data.get('pre_ipo_cost', 0)),
            'created_at': now,
            'updated_at': now,
        })


def get_candidate(code: str) -> Optional[Dict]:
    """查询单只 IPO 标的。"""
    with get_cursor() as cur:
        cur.execute('SELECT * FROM ipo_candidates WHERE code = ?', (code,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_candidates(
    status: Optional[str] = None,
    limit: int = 20,
) -> List[Dict]:
    """列出 IPO 候选标的。"""
    with get_cursor() as cur:
        if status:
            cur.execute(
                'SELECT * FROM ipo_candidates WHERE status = ? '
                'ORDER BY listing_date DESC LIMIT ?',
                (status, limit),
            )
        else:
            cur.execute(
                'SELECT * FROM ipo_candidates '
                'ORDER BY listing_date DESC LIMIT ?',
                (limit,),
            )
        return [dict(row) for row in cur.fetchall()]


# ─── Analysis CRUD ────────────────────────────────────────────

def save_analysis(data: Dict[str, Any]) -> None:
    """保存分析结果快照。"""
    import json
    with get_cursor() as cur:
        cur.execute('''
            INSERT INTO ipo_analyses
                (code, final_score, recommendation, heat_level, control_level,
                 sentiment_score, chips_score, narrative_score, valuation_score,
                 pricing_json, risk_alerts_json, key_factors_json, analyzed_at)
            VALUES
                (:code, :final_score, :recommendation, :heat_level, :control_level,
                 :sentiment_score, :chips_score, :narrative_score, :valuation_score,
                 :pricing_json, :risk_alerts_json, :key_factors_json, :analyzed_at)
            ON CONFLICT(code) DO UPDATE SET
                final_score = excluded.final_score,
                recommendation = excluded.recommendation,
                heat_level = excluded.heat_level,
                control_level = excluded.control_level,
                sentiment_score = excluded.sentiment_score,
                chips_score = excluded.chips_score,
                narrative_score = excluded.narrative_score,
                valuation_score = excluded.valuation_score,
                pricing_json = excluded.pricing_json,
                risk_alerts_json = excluded.risk_alerts_json,
                key_factors_json = excluded.key_factors_json,
                analyzed_at = excluded.analyzed_at
        ''', {
            'code': data['code'],
            'final_score': float(data.get('final_score', 0)),
            'recommendation': data.get('recommendation', ''),
            'heat_level': data.get('heat_level', ''),
            'control_level': data.get('control_level', ''),
            'sentiment_score': float(data.get('sentiment_score', 0)),
            'chips_score': float(data.get('chips_score', 0)),
            'narrative_score': float(data.get('narrative_score', 0)),
            'valuation_score': float(data.get('valuation_score', 0)),
            'pricing_json': json.dumps(data.get('pricing', []), ensure_ascii=False),
            'risk_alerts_json': json.dumps(data.get('risk_alerts', []), ensure_ascii=False),
            'key_factors_json': json.dumps(data.get('key_factors', []), ensure_ascii=False),
            'analyzed_at': data.get('analyzed_at', datetime.now().isoformat()),
        })


def get_analysis(code: str) -> Optional[Dict]:
    """查询分析结果。"""
    import json
    with get_cursor() as cur:
        cur.execute('SELECT * FROM ipo_analyses WHERE code = ?', (code,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d['pricing'] = json.loads(d.pop('pricing_json', '[]'))
        d['risk_alerts'] = json.loads(d.pop('risk_alerts_json', '[]'))
        d['key_factors'] = json.loads(d.pop('key_factors_json', '[]'))
        return d


# ─── Subscriptions CRUD ──────────────────────────────────────

def add_subscription(code: str, strategy: str = 'neutral') -> None:
    """订阅打新提醒。"""
    with get_cursor() as cur:
        cur.execute('''
            INSERT OR IGNORE INTO ipo_subscriptions (code, strategy, subscribed_at)
            VALUES (?, ?, ?)
        ''', (code, strategy, datetime.now().isoformat()))


def list_subscriptions() -> List[Dict]:
    """查看已订阅列表。"""
    with get_cursor() as cur:
        cur.execute(
            'SELECT * FROM ipo_subscriptions ORDER BY subscribed_at DESC'
        )
        return [dict(row) for row in cur.fetchall()]


def remove_subscription(code: str, strategy: str = 'neutral') -> bool:
    """取消订阅。"""
    with get_cursor() as cur:
        cur.execute(
            'DELETE FROM ipo_subscriptions WHERE code = ? AND strategy = ?',
            (code, strategy),
        )
        return cur.rowcount > 0

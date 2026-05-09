"""
core/audit_log.py — 合规审计日志（Append-Only JSON Lines）

每笔交易触发时自动记录：
  - 时间戳、订单 ID、标的、方向、价格、数量
  - 信号来源（因子名、combined_score）
  - 因子值快照（各因子的 latest_value）
  - 风控检查结果（passed / reason）
  - 执行结果（成交价、佣金、滑点）

格式：JSON Lines（每行一个 JSON 对象，只追加不修改）
路径：outputs/audit/{YYYY-MM}/audit_{YYYY-MM-DD}.jsonl

不可变性保证：
  - 写入时使用 'a'（追加）模式，不覆盖
  - 每条记录含 SHA-256 内容哈希字段（entry_hash），可检测篡改

用法::

    from core.audit_log import AuditLogger, AuditEntry

    logger = AuditLogger()

    entry = AuditEntry(
        order_id='ord-001',
        symbol='000001.SZ',
        direction='BUY',
        price=15.0,
        shares=100,
        signal_source='FactorPipeline',
        combined_score=0.72,
        factor_values={'RSI': -1.2, 'MACD': 0.8},
        risk_passed=True,
        risk_reason='',
        fill_price=15.02,
        commission=4.51,
        slippage_bps=1.3,
    )
    logger.write(entry)
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_AUDIT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'outputs', 'audit'
)


@dataclass
class AuditEntry:
    """单条合规审计记录。"""
    # 订单基本信息
    order_id: str
    symbol: str
    direction: str              # 'BUY' | 'SELL'
    price: float                # 提交价格
    shares: int

    # 信号来源
    signal_source: str          # 因子名 / 'StrategyRunner' / 'IntradayMonitor'
    combined_score: float       # FactorPipeline 合成分数

    # 因子值快照（各因子 latest_value）
    factor_values: Dict[str, float] = field(default_factory=dict)

    # 风控检查结果
    risk_passed: bool = True
    risk_reason: str = ''

    # 执行结果（成交后填入）
    fill_price: float = 0.0
    commission: float = 0.0
    slippage_bps: float = 0.0

    # 自动填充字段（write() 时生成）
    timestamp: str = ''         # ISO 8601（UTC）
    entry_hash: str = ''        # SHA-256 内容哈希（防篡改）

    def _compute_hash(self) -> str:
        """计算记录内容的 SHA-256 哈希（不含 entry_hash 字段本身）。"""
        d = asdict(self)
        d.pop('entry_hash', None)
        canonical = json.dumps(d, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def finalize(self) -> 'AuditEntry':
        """填充 timestamp 和 entry_hash，返回 self 以支持链式调用。"""
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.entry_hash = self._compute_hash()
        return self


class AuditLogger:
    """
    合规审计日志写入器。

    线程安全：使用文件追加模式，多进程环境下依赖 OS 的原子 write 保证。
    """

    def __init__(self, audit_dir: str = _AUDIT_DIR) -> None:
        self.audit_dir = audit_dir

    def _log_path(self, dt: Optional[datetime] = None) -> str:
        if dt is None:
            dt = datetime.now()
        month_dir = os.path.join(self.audit_dir, dt.strftime('%Y-%m'))
        os.makedirs(month_dir, exist_ok=True)
        return os.path.join(month_dir, f'audit_{dt.strftime("%Y-%m-%d")}.jsonl')

    def write(self, entry: AuditEntry) -> str:
        """
        写入一条审计记录（追加模式）。

        Returns
        -------
        str
            写入的文件路径
        """
        entry.finalize()
        path = self._log_path()
        line = json.dumps(asdict(entry), ensure_ascii=False)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
        return path

    def read(self, date_str: Optional[str] = None) -> List[AuditEntry]:
        """
        读取指定日期的审计记录（默认今日）。

        Parameters
        ----------
        date_str : str, optional
            'YYYY-MM-DD'，默认当天
        """
        if date_str:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
        else:
            dt = datetime.now()

        path = self._log_path(dt)
        if not os.path.exists(path):
            return []

        entries = []
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    entries.append(AuditEntry(**d))
                except Exception:
                    pass
        return entries

    def verify(self, date_str: Optional[str] = None) -> Dict[str, int]:
        """
        验证审计记录完整性（重新计算 entry_hash 与存储值对比）。

        Returns
        -------
        dict
            {'total': n, 'ok': n, 'tampered': n}
        """
        entries = self.read(date_str)
        total = len(entries)
        ok = 0
        tampered = 0
        for entry in entries:
            stored_hash = entry.entry_hash
            entry.entry_hash = ''
            expected = entry._compute_hash()
            if stored_hash == expected:
                ok += 1
            else:
                tampered += 1
        return {'total': total, 'ok': ok, 'tampered': tampered}

    def list_dates(self) -> List[str]:
        """列出所有有审计记录的日期（'YYYY-MM-DD'）。"""
        dates = []
        if not os.path.exists(self.audit_dir):
            return dates
        for month_dir in sorted(os.listdir(self.audit_dir)):
            month_path = os.path.join(self.audit_dir, month_dir)
            if not os.path.isdir(month_path):
                continue
            for fname in sorted(os.listdir(month_path)):
                if fname.startswith('audit_') and fname.endswith('.jsonl'):
                    dates.append(fname[6:16])  # 'audit_YYYY-MM-DD.jsonl' → 'YYYY-MM-DD'
        return dates


# ---------------------------------------------------------------------------
# OMS 集成助手（在 OMS._on_signal 成交成功后调用）
# ---------------------------------------------------------------------------

_default_audit_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    """返回全局 AuditLogger 单例。"""
    global _default_audit_logger
    if _default_audit_logger is None:
        _default_audit_logger = AuditLogger()
    return _default_audit_logger


# ---------------------------------------------------------------------------
# P2-18: 通用事件审计（订单取消 / 强平 / 参数变更 / ML 重训）
# ---------------------------------------------------------------------------

# 通用事件 JSONL 写入到同一个 audit_dir，kind 字段区分类型。
# - kind='order_fill'      → AuditEntry（log_fill 写入）
# - kind='order_cancel'    → 订单取消（含 reason / origin）
# - kind='liquidation'     → 强平触发（含 trigger / drawdown / equity）
# - kind='param_change'    → 参数 / 配置变更（before/after）
# - kind='ml_retrain'      → ML 模型重训（含 oos_acc / sharpe / 是否落盘）

def _log_event(kind: str, payload: Dict[str, Any]) -> Optional[str]:
    """通用事件审计：写入与 AuditEntry 相同目录的 jsonl 文件。"""
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
        record = {
            'kind': kind,
            'timestamp': ts,
            **payload,
        }
        # 哈希指纹（kind + payload，去掉 timestamp 让相同业务事件可比）
        canonical = json.dumps({k: v for k, v in record.items() if k != 'timestamp'},
                               sort_keys=True, ensure_ascii=False)
        record['entry_hash'] = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        logger = get_audit_logger()
        path = logger._log_path()
        line = json.dumps(record, ensure_ascii=False)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
        return path
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger('core.audit_log').error('[AuditLog] event 写入失败 (%s): %s',
                                                    kind, exc)
        return None


def log_order_cancel(order_id: str, symbol: str, reason: str,
                     origin: str = 'manual',
                     extra: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """记录订单取消。origin: 'manual' / 'risk_engine' / 'exit_engine' / 'broker'。"""
    return _log_event('order_cancel', {
        'order_id': order_id,
        'symbol': symbol,
        'reason': reason,
        'origin': origin,
        **(extra or {}),
    })


def log_liquidation(symbol: str, trigger: str, drawdown_pct: float,
                    equity: float, shares: int,
                    extra: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """记录强平触发。trigger: 'P0_emergency_stop' / 'max_drawdown_breach' / 'circuit_break'。"""
    return _log_event('liquidation', {
        'symbol': symbol,
        'trigger': trigger,
        'drawdown_pct': round(float(drawdown_pct), 6),
        'equity': round(float(equity), 2),
        'shares': int(shares),
        **(extra or {}),
    })


def log_param_change(component: str, key: str, before: Any, after: Any,
                     changed_by: str = 'system') -> Optional[str]:
    """记录参数 / 配置变更。component='RiskEngine' / 'OMS' / 'PortfolioOptimizer' 等。"""
    return _log_event('param_change', {
        'component': component,
        'key': key,
        'before': before,
        'after': after,
        'changed_by': changed_by,
    })


def log_ml_retrain(symbol: str, model: str, oos_accuracy: float,
                   oos_sharpe: float, persisted: bool,
                   reason: str = '',
                   extra: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """记录 ML 模型重训。reason: 'scheduled' / 'oos_below_threshold' / 'manual'。"""
    return _log_event('ml_retrain', {
        'symbol': symbol,
        'model': model,
        'oos_accuracy': round(float(oos_accuracy), 6),
        'oos_sharpe': round(float(oos_sharpe), 6),
        'persisted': bool(persisted),
        'reason': reason,
        **(extra or {}),
    })


def log_fill(
    fill,
    signal=None,
    pipeline_result=None,
    risk_passed: bool = True,
    risk_reason: str = '',
) -> Optional[str]:
    """
    从 OMS Fill + Signal + PipelineResult 快速写入一条审计记录。

    Parameters
    ----------
    fill : Fill
        OMS 成交回报对象（含 order_id, symbol, direction, price, shares, commission）
    signal : Signal, optional
        触发信号（含 factor_name, price, strength）
    pipeline_result : PipelineResult, optional
        因子流水线结果（含 combined_score, factor_results）
    risk_passed : bool
        是否通过风控
    risk_reason : str
        风控拒绝原因（通过时为空）

    Returns
    -------
    str or None
        写入的审计日志文件路径；失败时返回 None
    """
    try:
        factor_values: Dict[str, float] = {}
        combined_score = 0.0
        signal_source = 'unknown'

        if pipeline_result is not None:
            combined_score = float(getattr(pipeline_result, 'combined_score', 0.0))
            for fr in getattr(pipeline_result, 'factor_results', []):
                factor_values[fr.name] = round(float(fr.latest_value or 0.0), 6)

        if signal is not None:
            signal_source = getattr(signal, 'factor_name', 'StrategyRunner')

        entry = AuditEntry(
            order_id=getattr(fill, 'order_id', ''),
            symbol=getattr(fill, 'symbol', ''),
            direction=getattr(fill, 'direction', ''),
            price=float(getattr(fill, 'price', 0.0)),
            shares=int(getattr(fill, 'shares', 0)),
            signal_source=signal_source,
            combined_score=combined_score,
            factor_values=factor_values,
            risk_passed=risk_passed,
            risk_reason=risk_reason,
            fill_price=float(getattr(fill, 'price', 0.0)),
            commission=float(getattr(fill, 'commission', 0.0)),
            slippage_bps=0.0,  # 由调用方填入
        )
        return get_audit_logger().write(entry)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger('core.audit_log').error('[AuditLog] 写入失败: %s', exc)
        return None

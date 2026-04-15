"""
core/brokers/facade.py — Broker Factory + SafetyMode

SafetyMode:
  PAPER      — Only PaperBroker, all real order paths blocked (current phase)
  SIMULATED  — Same as PAPER, semantic distinction
  LIVE       — Real orders allowed (requires 3-step unlock)

BrokerFactory:
  Creates BrokerAdapter based on config.
  In PAPER mode, forces PaperBroker even if other broker is configured.

All real broker send/cancel methods check SafetyMode and raise
BrokerSecurityError in PAPER mode, ensuring absolute safety.
"""

from __future__ import annotations
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Dict, Optional, Any
import os

from core.oms import BrokerAdapter


class SafetyMode(Enum):
    PAPER = auto()
    SIMULATED = auto()
    LIVE = auto()

    def is_safe(self) -> bool:
        return self in (SafetyMode.PAPER, SafetyMode.SIMULATED)


class BrokerSecurityError(Exception):
    """Raised when a real broker operation is blocked by SafetyMode."""
    pass


class BrokerFactory:
    """
    Broker factory. Creates the configured BrokerAdapter.
    Config resolution priority: env > config file > default PAPER
    """

    DEFAULT_CONFIG_PATH = os.path.join(
        os.path.dirname(__file__), '..', '..', 'config', 'brokers.json'
    )

    _instance: Optional['BrokerFactory'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized') and self._initialized:
            return
        self._initialized = True
        self._mode = self._resolve_mode()
        self._broker: Optional[BrokerAdapter] = None

    def _resolve_mode(self) -> SafetyMode:
        env = os.environ.get('QUANT_BROKER_MODE', '').strip().upper()
        if env in ('PAPER', 'SIMULATED'):
            return SafetyMode.PAPER

        cfg_path = os.environ.get('QUANT_BROKER_CONFIG', self.DEFAULT_CONFIG_PATH)
        try:
            import json
            if os.path.exists(cfg_path):
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                mode_str = cfg.get('safety_mode', 'PAPER').upper()
                if mode_str == 'LIVE' and self._check_live_unlock():
                    return SafetyMode.LIVE
                return SafetyMode.PAPER
        except Exception:
            pass
        return SafetyMode.PAPER

    def _check_live_unlock(self) -> bool:
        cfg_path = os.environ.get('QUANT_BROKER_CONFIG', self.DEFAULT_CONFIG_PATH)
        try:
            import json
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if cfg.get('safety_mode', '').upper() != 'LIVE':
                return False
        except Exception:
            return False
        step1 = os.environ.get('QUANT_LIVE_CONFIRM', '').strip() == '1'
        step2 = os.path.exists(os.path.join(os.path.dirname(cfg_path), 'live_armed'))
        return step1 and step2

    @property
    def mode(self) -> SafetyMode:
        return self._mode

    @property
    def mode_label(self) -> str:
        labels = {
            SafetyMode.PAPER: '[PAPER] Simulated trading only',
            SafetyMode.SIMULATED: '[SIMULATED] Simulated matching',
            SafetyMode.LIVE: '[LIVE] Real orders enabled',
        }
        return labels[self.mode]

    def require_live(self):
        """
        Attempt to unlock LIVE mode.
        Raises BrokerSecurityError if 3-step unlock is not complete.
        """
        if self._check_live_unlock():
            self._mode = SafetyMode.LIVE
            print('[BrokerFactory] WARNING: LIVE mode unlocked')
            return
        raise BrokerSecurityError(
            'LIVE mode not unlocked. Requires: '
            '(1) config/brokers.json with safety_mode=LIVE, '
            '(2) env QUANT_LIVE_CONFIRM=1, '
            '(3) file config/live_armed'
        )

    def get_broker(self) -> BrokerAdapter:
        """Get the configured BrokerAdapter (singleton per factory)."""
        if self._broker is not None:
            return self._broker

        if self._mode in (SafetyMode.PAPER, SafetyMode.SIMULATED):
            self._broker = self._create_paper_broker()
        elif self._mode == SafetyMode.LIVE:
            self._broker = self._create_live_broker()

        print(f'[BrokerFactory] Broker: {self._broker.name} | Mode: {self.mode_label}')
        return self._broker

    def _create_paper_broker(self):
        from core.brokers.paper import PaperBroker
        return PaperBroker()

    def _create_live_broker(self):
        cfg_path = os.environ.get('QUANT_BROKER_CONFIG', self.DEFAULT_CONFIG_PATH)
        try:
            import json
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception:
            print('[BrokerFactory] No broker config found, using PaperBroker')
            return self._create_paper_broker()

        broker = cfg.get('broker', '').lower()
        if broker == 'futu':
            from core.brokers.futu import FutuBroker
            return FutuBroker(
                host=cfg.get('futu_host', '127.0.0.1'),
                port=cfg.get('futu_port', 11111),
            )
        elif broker == 'tiger':
            from core.brokers.tiger import TigerBroker
            return TigerBroker(
                tiger_id=cfg.get('tiger_id', ''),
                account=cfg.get('tiger_account', ''),
            )
        elif broker == 'ibkr':
            from core.brokers.ibkr import IBBroker
            return IBBroker(
                host=cfg.get('ibkr_host', '127.0.0.1'),
                port=cfg.get('ibkr_port', 4001),
            )
        else:
            print(f'[BrokerFactory] Unknown broker "{broker}", using PaperBroker')
            return self._create_paper_broker()

    def assert_safe(self, operation: str = 'order'):
        """
        Safety check: must be called before any real order operation.
        Raises BrokerSecurityError if in PAPER mode.
        """
        if self._mode in (SafetyMode.PAPER, SafetyMode.SIMULATED):
            raise BrokerSecurityError(
                f'Safety mode blocked "{operation}". '
                f'Current mode: {self.mode_label}. '
                f'Call BrokerFactory().require_live() to unlock.'
            )


def get_broker_factory() -> BrokerFactory:
    return BrokerFactory()


def create_broker() -> BrokerAdapter:
    """Convenience: get the configured broker."""
    return BrokerFactory().get_broker()

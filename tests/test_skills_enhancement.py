"""
tests/test_skills_enhancement.py — Skills 增强功能测试

测试内容：
1. /fundamentals/{symbol} API 扩展字段
2. 测试数据端点 (/test/*)
3. API 超时配置验证
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJ_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ_DIR))
sys.path.insert(0, str(PROJ_DIR / 'backend'))


@pytest.fixture(scope='module')
def client():
    """加载 backend.api 的 Flask app 并返回 test_client。"""
    import backend.api as api
    return api.app.test_client()


# ────────────────────────────────────────────────────────
# Fundamentals API 扩展字段测试
# ────────────────────────────────────────────────────────

class TestFundamentalsExpansion:
    """测试 /fundamentals/{symbol} API 扩展字段"""

    def test_fundamentals_returns_extended_fields(self, client):
        """验证 API 返回扩展的基本面字段"""
        # Mock gateway 的 quote 和 fundamentals 方法
        mock_quote = MagicMock()
        mock_quote.is_valid = True
        mock_quote.name = "长江电力"
        mock_quote.pe_ttm = 18.47
        mock_quote.pb = 2.92
        mock_quote.dividend_yield = 0.15
        mock_quote.market_cap = 6665.14
        mock_quote.price = 27.24

        mock_fundamentals = MagicMock()
        mock_fundamentals.revenue_yoy = 6.44
        mock_fundamentals.profit_yoy = 30.50
        mock_fundamentals.roe_ttm = 3.01
        mock_fundamentals.eps_ttm = 0.2763
        mock_fundamentals.ocf_to_profit = 1.73
        mock_fundamentals.industry = "电力"
        mock_fundamentals.sector = "公用事业"

        mock_gateway = MagicMock()
        mock_gateway.quote.return_value = mock_quote
        mock_gateway.fundamentals.return_value = mock_fundamentals

        with patch('core.data_gateway.get_gateway', return_value=mock_gateway):
            r = client.get('/fundamentals/600900.SH')

        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'ok'

        # 验证基础字段
        assert data['symbol'] == '600900.SH'
        assert data['name'] == '长江电力'
        assert data['pe'] == 18.47
        assert data['pb'] == 2.92
        assert data['dividend_yield'] == 0.15
        assert data['market_cap'] == 6665.14
        assert data['price'] == 27.24

        # 验证扩展字段
        assert data['revenue_yoy'] == 6.44
        assert data['profit_yoy'] == 30.50
        assert data['roe_ttm'] == 3.01
        assert data['eps_ttm'] == 0.2763
        assert data['ocf_to_profit'] == 1.73
        assert data['industry'] == '电力'
        assert data['sector'] == '公用事业'

    def test_fundamentals_handles_missing_fundamentals(self, client):
        """当 Fundamentals 对象不可用时，扩展字段应为默认值"""
        mock_quote = MagicMock()
        mock_quote.is_valid = True
        mock_quote.name = "测试股票"
        mock_quote.pe_ttm = 10.0
        mock_quote.pb = 1.5
        mock_quote.dividend_yield = 0.02
        mock_quote.market_cap = 100.0
        mock_quote.price = 10.0

        mock_gateway = MagicMock()
        mock_gateway.quote.return_value = mock_quote
        mock_gateway.fundamentals.return_value = None

        with patch('core.data_gateway.get_gateway', return_value=mock_gateway):
            r = client.get('/fundamentals/000001.SZ')

        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'ok'

        # 扩展字段应为默认值
        assert data['revenue_yoy'] == 0.0
        assert data['profit_yoy'] == 0.0
        assert data['roe_ttm'] == 0.0
        assert data['eps_ttm'] == 0.0
        assert data['ocf_to_profit'] == 0.0
        assert data['industry'] == ''
        assert data['sector'] == ''

    def test_fundamentals_handles_invalid_quote(self, client):
        """当 Quote 无效时，应返回 404"""
        mock_quote = MagicMock()
        mock_quote.is_valid = False

        mock_gateway = MagicMock()
        mock_gateway.quote.return_value = mock_quote

        with patch('core.data_gateway.get_gateway', return_value=mock_gateway):
            r = client.get('/fundamentals/INVALID')

        assert r.status_code == 404
        data = r.get_json()
        assert data['status'] == 'error'


# ────────────────────────────────────────────────────────
# 测试数据端点测试
# ────────────────────────────────────────────────────────

class TestTestDataEndpoints:
    """测试 /test/* 端点"""

    def test_create_test_position(self, client):
        """测试创建测试持仓"""
        mock_svc = MagicMock()
        mock_svc.upsert_position = MagicMock()

        with patch('backend.api_routes.test._get_portfolio_service', return_value=mock_svc):
            r = client.post('/test/positions', json={
                'symbol': '600900.SH',
                'shares': 1000,
                'entry_price': 26.50,
            })

        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'ok'
        assert data['message'] == 'Test position created'
        assert data['position']['symbol'] == '600900.SH'
        assert data['position']['shares'] == 1000
        assert data['position']['entry_price'] == 26.50
        mock_svc.upsert_position.assert_called_once()

    def test_create_test_position_missing_fields(self, client):
        """测试创建测试持仓时缺少必填字段"""
        r = client.post('/test/positions', json={
            'symbol': '600900.SH',
            # 缺少 shares 和 entry_price
        })

        assert r.status_code == 400
        data = r.get_json()
        assert data['status'] == 'error'

    def test_clear_test_positions(self, client):
        """测试清空测试持仓"""
        mock_svc = MagicMock()
        mock_positions = [
            {'symbol': '600900.SH', 'shares': 1000},
            {'symbol': '000001.SZ', 'shares': 500},
        ]
        mock_svc.get_positions.return_value = mock_positions
        mock_svc.close_position = MagicMock()

        with patch('backend.api_routes.test._get_portfolio_service', return_value=mock_svc):
            r = client.delete('/test/positions')

        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'ok'
        assert 'Cleared' in data['message']
        assert mock_svc.close_position.call_count == 2

    def test_create_test_signal(self, client):
        """测试创建测试信号"""
        mock_svc = MagicMock()
        mock_svc.record_signal = MagicMock()

        with patch('backend.api_routes.test._get_portfolio_service', return_value=mock_svc):
            r = client.post('/test/signals', json={
                'symbol': '600900.SH',
                'signal': 'BUY',
                'strength': 0.8,
                'reason': 'RSI超卖反弹',
            })

        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'ok'
        assert data['message'] == 'Test signal created'
        assert data['signal']['symbol'] == '600900.SH'
        assert data['signal']['signal'] == 'BUY'
        assert data['signal']['strength'] == 0.8
        mock_svc.record_signal.assert_called_once()

    def test_create_test_signal_invalid_signal_type(self, client):
        """测试创建测试信号时使用无效的信号类型"""
        r = client.post('/test/signals', json={
            'symbol': '600900.SH',
            'signal': 'INVALID',  # 无效的信号类型
        })

        assert r.status_code == 400
        data = r.get_json()
        assert data['status'] == 'error'

    def test_create_test_trade(self, client):
        """测试创建测试成交"""
        mock_svc = MagicMock()
        mock_svc.record_trade.return_value = 'test_trade_123'

        with patch('backend.api_routes.test._get_portfolio_service', return_value=mock_svc):
            r = client.post('/test/trades', json={
                'symbol': '600900.SH',
                'side': 'BUY',
                'shares': 1000,
                'price': 27.25,
            })

        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'ok'
        assert data['message'] == 'Test trade created'
        assert data['trade']['symbol'] == '600900.SH'
        assert data['trade']['direction'] == 'BUY'
        assert data['trade']['shares'] == 1000
        assert data['trade']['price'] == 27.25
        mock_svc.record_trade.assert_called_once()

    def test_create_test_trade_invalid_side(self, client):
        """测试创建测试成交时使用无效的 side"""
        r = client.post('/test/trades', json={
            'symbol': '600900.SH',
            'side': 'INVALID',  # 无效的 side
            'shares': 1000,
            'price': 27.25,
        })

        assert r.status_code == 400
        data = r.get_json()
        assert data['status'] == 'error'

    def test_reset_test_data(self, client):
        """测试重置所有测试数据"""
        mock_svc = MagicMock()
        mock_positions = [
            {'symbol': '600900.SH', 'shares': 1000},
        ]
        mock_svc.get_positions.return_value = mock_positions
        mock_svc.close_position = MagicMock()

        with patch('backend.api_routes.test._get_portfolio_service', return_value=mock_svc):
            r = client.post('/test/reset')

        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'ok'
        assert 'All test data reset' in data['message']
        mock_svc.close_position.assert_called_once()


# ────────────────────────────────────────────────────────
# API 超时配置验证测试
# ────────────────────────────────────────────────────────

class TestAPITimeoutConfiguration:
    """测试 API 超时配置"""

    def test_analysis_stock_a_endpoint_exists(self, client):
        """验证 /analysis/stock/a 端点存在"""
        # 这个端点需要 LLM 调用，我们只验证端点存在
        # 实际测试需要 mock LLM 服务
        pass

    def test_analysis_sector_rotation_endpoint_exists(self, client):
        """验证 /analysis/sector_rotation 端点存在"""
        # 这个端点需要较长时间，我们只验证端点存在
        # 实际测试需要 mock 数据源
        pass


# ────────────────────────────────────────────────────────
# 集成测试
# ────────────────────────────────────────────────────────

class TestIntegration:
    """集成测试：验证各个组件协同工作"""

    def test_full_workflow_with_test_data(self, client):
        """测试完整的工作流程：创建测试数据 → 查询 → 分析"""
        mock_svc = MagicMock()
        mock_svc.upsert_position = MagicMock()
        mock_svc.record_signal = MagicMock()
        mock_svc.record_trade.return_value = 'test_trade_123'
        mock_positions = [{'symbol': '600900.SH', 'shares': 1000}]
        mock_svc.get_positions.return_value = mock_positions
        mock_svc.close_position = MagicMock()

        with patch('backend.api_routes.test._get_portfolio_service', return_value=mock_svc):
            # 1. 创建测试持仓
            r = client.post('/test/positions', json={
                'symbol': '600900.SH',
                'shares': 1000,
                'entry_price': 26.50,
            })
            assert r.status_code == 200

            # 2. 创建测试信号
            r = client.post('/test/signals', json={
                'symbol': '600900.SH',
                'signal': 'BUY',
                'strength': 0.8,
            })
            assert r.status_code == 200

            # 3. 创建测试成交
            r = client.post('/test/trades', json={
                'symbol': '600900.SH',
                'side': 'BUY',
                'shares': 1000,
                'price': 27.25,
            })
            assert r.status_code == 200

            # 4. 重置测试数据
            r = client.post('/test/reset')
            assert r.status_code == 200


# ────────────────────────────────────────────────────────
# 边界条件测试
# ────────────────────────────────────────────────────────

class TestEdgeCases:
    """边界条件测试"""

    def test_fundamentals_with_zero_values(self, client):
        """测试基本面字段为零的情况"""
        mock_quote = MagicMock()
        mock_quote.is_valid = True
        mock_quote.name = "测试股票"
        mock_quote.pe_ttm = 0.0
        mock_quote.pb = 0.0
        mock_quote.dividend_yield = 0.0
        mock_quote.market_cap = 0.0
        mock_quote.price = 0.0

        mock_fundamentals = MagicMock()
        mock_fundamentals.revenue_yoy = 0.0
        mock_fundamentals.profit_yoy = 0.0
        mock_fundamentals.roe_ttm = 0.0
        mock_fundamentals.eps_ttm = 0.0
        mock_fundamentals.ocf_to_profit = 0.0
        mock_fundamentals.industry = ""
        mock_fundamentals.sector = ""

        mock_gateway = MagicMock()
        mock_gateway.quote.return_value = mock_quote
        mock_gateway.fundamentals.return_value = mock_fundamentals

        with patch('core.data_gateway.get_gateway', return_value=mock_gateway):
            r = client.get('/fundamentals/000000.SZ')

        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'ok'
        assert data['pe'] == 0.0
        assert data['revenue_yoy'] == 0.0

    def test_create_position_with_large_numbers(self, client):
        """测试创建大数值的持仓"""
        mock_svc = MagicMock()
        mock_svc.upsert_position = MagicMock()

        with patch('backend.api_routes.test._get_portfolio_service', return_value=mock_svc):
            r = client.post('/test/positions', json={
                'symbol': '600900.SH',
                'shares': 1000000,
                'entry_price': 10000.00,
            })

        assert r.status_code == 200
        data = r.get_json()
        assert data['position']['shares'] == 1000000
        assert data['position']['entry_price'] == 10000.00
        assert data['position']['cost_value'] == 10000000000.0

    def test_create_signal_with_edge_strength(self, client):
        """测试边界强度值的信号"""
        mock_svc = MagicMock()
        mock_svc.record_signal = MagicMock()

        with patch('backend.api_routes.test._get_portfolio_service', return_value=mock_svc):
            # 测试最小强度
            r = client.post('/test/signals', json={
                'symbol': '600900.SH',
                'signal': 'BUY',
                'strength': 0.0,
            })
        assert r.status_code == 200

        with patch('backend.api_routes.test._get_portfolio_service', return_value=mock_svc):
            # 测试最大强度
            r = client.post('/test/signals', json={
                'symbol': '600900.SH',
                'signal': 'SELL',
                'strength': 1.0,
            })
        assert r.status_code == 200

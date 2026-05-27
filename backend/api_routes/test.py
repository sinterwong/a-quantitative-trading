"""
测试数据端点模块

提供测试数据创建和管理功能，用于开发和测试环境。
仅在开发模式下可用。
"""

from __future__ import annotations

from datetime import datetime
from flask import Blueprint, request
from backend.api import err, ok

test_bp = Blueprint('test', __name__)


def _get_portfolio_service():
    """获取 PortfolioService 实例"""
    from backend.services.portfolio import PortfolioService
    return PortfolioService()


@test_bp.route('/test/positions', methods=['POST'])
def create_test_position():
    """
    POST /test/positions — 创建测试持仓数据
    
    Request Body:
    {
        "symbol": "600900.SH",
        "shares": 1000,
        "entry_price": 26.50,
        "entry_date": "2026-01-15"  // optional
    }
    
    Returns:
        创建的持仓信息
    """
    try:
        data = request.get_json()
        if not data:
            return err('Request body is required', 400)
        
        symbol = data.get('symbol')
        shares = data.get('shares')
        entry_price = data.get('entry_price')
        
        if not all([symbol, shares, entry_price]):
            return err('symbol, shares, and entry_price are required', 400)
        
        # 使用 PortfolioService 添加持仓
        svc = _get_portfolio_service()
        svc.upsert_position(
            symbol=symbol,
            shares=int(shares),
            entry_price=float(entry_price),
            latest_price=float(entry_price),  # 初始价格等于入场价格
        )
        
        position = {
            'symbol': symbol,
            'shares': int(shares),
            'entry_price': float(entry_price),
            'cost_value': int(shares) * float(entry_price),
            'created_at': datetime.now().isoformat(),
        }
        
        return ok(message='Test position created', position=position)
        
    except Exception as e:
        return err(f'Failed to create test position: {str(e)}', 500)


@test_bp.route('/test/positions', methods=['DELETE'])
def clear_test_positions():
    """
    DELETE /test/positions — 清空所有测试持仓
    
    Returns:
        操作结果
    """
    try:
        svc = _get_portfolio_service()
        positions = svc.get_positions()
        
        # 关闭所有持仓
        for p in positions:
            svc.close_position(p['symbol'])
        
        return ok(message=f'Cleared {len(positions)} positions')
        
    except Exception as e:
        return err(f'Failed to clear test positions: {str(e)}', 500)


@test_bp.route('/test/signals', methods=['POST'])
def create_test_signal():
    """
    POST /test/signals — 创建测试信号
    
    Request Body:
    {
        "symbol": "600900.SH",
        "signal": "BUY",  // BUY or SELL
        "strength": 0.8,
        "reason": "Test signal"
    }
    
    Returns:
        创建的信号信息
    """
    try:
        data = request.get_json()
        if not data:
            return err('Request body is required', 400)
        
        symbol = data.get('symbol')
        signal = data.get('signal')
        strength = data.get('strength', 0.5)
        reason = data.get('reason', 'Test signal')
        
        if not all([symbol, signal]):
            return err('symbol and signal are required', 400)
        
        if signal not in ['BUY', 'SELL']:
            return err('signal must be BUY or SELL', 400)
        
        # 使用 PortfolioService 记录信号
        svc = _get_portfolio_service()
        svc.record_signal(
            symbol=symbol,
            signal=signal,
            strength=float(strength),
            reason=reason,
        )
        
        signal_data = {
            'symbol': symbol,
            'signal': signal,
            'strength': float(strength),
            'reason': reason,
            'timestamp': datetime.now().isoformat(),
        }
        
        return ok(message='Test signal created', signal=signal_data)
        
    except Exception as e:
        return err(f'Failed to create test signal: {str(e)}', 500)


@test_bp.route('/test/trades', methods=['POST'])
def create_test_trade():
    """
    POST /test/trades — 创建测试成交记录
    
    Request Body:
    {
        "symbol": "600900.SH",
        "side": "BUY",  // BUY or SELL
        "shares": 1000,
        "price": 27.25,
        "order_id": "test_order_123"  // optional
    }
    
    Returns:
        创建的成交记录
    """
    try:
        data = request.get_json()
        if not data:
            return err('Request body is required', 400)
        
        symbol = data.get('symbol')
        side = data.get('side')
        shares = data.get('shares')
        price = data.get('price')
        
        if not all([symbol, side, shares, price]):
            return err('symbol, side, shares, and price are required', 400)
        
        if side not in ['BUY', 'SELL']:
            return err('side must be BUY or SELL', 400)
        
        # 使用 PortfolioService 记录交易
        svc = _get_portfolio_service()
        trade_id = svc.record_trade(
            symbol=symbol,
            direction=side,
            shares=int(shares),
            price=float(price),
        )
        
        trade = {
            'trade_id': trade_id,
            'symbol': symbol,
            'direction': side,
            'shares': int(shares),
            'price': float(price),
            'amount': int(shares) * float(price),
            'executed_at': datetime.now().isoformat(),
        }
        
        return ok(message='Test trade created', trade=trade)
        
    except Exception as e:
        return err(f'Failed to create test trade: {str(e)}', 500)


@test_bp.route('/test/reset', methods=['POST'])
def reset_test_data():
    """
    POST /test/reset — 重置所有测试数据
    
    Returns:
        操作结果
    """
    try:
        svc = _get_portfolio_service()
        
        # 清空持仓
        positions = svc.get_positions()
        for p in positions:
            svc.close_position(p['symbol'])
        
        return ok(
            message='All test data reset',
            cleared_positions=len(positions),
        )
        
    except Exception as e:
        return err(f'Failed to reset test data: {str(e)}', 500)

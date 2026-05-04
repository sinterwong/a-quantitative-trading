"""
scheduler/ipo_scanner.py — Phase 7 IPO Stars 定时扫描任务

功能：
  1. 每日 09:00 扫描港交所新股列表（东方财富数据源）
  2. 对比 IPORecordStore，已分析的跳过（幂等）
  3. 发现新招股 → 全量分析 → 推送飞书报告
  4. 跟踪招股状态变化（聆讯→申购→上市）

Usage:
  # 定时任务（cron）
  0 9 * * 1-5 cd ~/workspace/a-quantitative-trading-xh && python -m scheduler.ipo_scanner

  # 手动触发单只
  from scheduler.ipo_scanner import IPOScanner
  result = IPOScanner().analyze_one('09619')
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import sys
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

# ── 路径兼容 ─────────────────────────────────────────────────────────────────

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(THIS_DIR)
BACKEND_DIR = os.path.join(PROJECT_DIR, 'backend')
sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, PROJECT_DIR)

_log = logging.getLogger('ipo_scanner')

# ── 飞书推送 ─────────────────────────────────────────────────────────────────

_FEISHU_APP_ID = os.environ.get('FEISHU_APP_ID', '')
_FEISHU_APP_SECRET = os.environ.get('FEISHU_APP_SECRET', '')
_FEISHU_USER_ID = os.environ.get('FEISHU_USER_ID', 'ou_b8add658ac094464606af32933a02d0b')


def _feishu_push(text: str) -> bool:
    """推送文本到飞书。失败返回 False，不抛异常。"""
    if not _FEISHU_APP_ID or not _FEISHU_APP_SECRET:
        _log.debug('Feishu not configured, skipping push')
        return False

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        # 获取 token
        token_url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
        token_data = json.dumps({
            'app_id': _FEISHU_APP_ID,
            'app_secret': _FEISHU_APP_SECRET,
        }).encode()
        tok_req = urllib.request.Request(
            token_url, data=token_data,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(tok_req, context=ctx, timeout=10) as r:
            token_result = json.loads(r.read())
        token = token_result.get('tenant_access_token', '')
        if not token:
            _log.warning('Feishu: failed to get tenant token')
            return False

        # 发送消息
        msg_url = 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id'
        msg_body = {
            'receive_id': _FEISHU_USER_ID,
            'msg_type': 'text',
            'content': json.dumps({'text': text}),
        }
        msg_req = urllib.request.Request(
            msg_url,
            data=json.dumps(msg_body).encode(),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {token}',
            },
            method='POST',
        )
        with urllib.request.urlopen(msg_req, context=ctx, timeout=10) as r:
            resp = json.loads(r.read())
            if resp.get('code') == 0 or resp.get('status') == 0:
                _log.info('Feishu push success')
                return True
            else:
                _log.warning('Feishu push failed: %s', resp)
                return False
    except Exception as e:
        _log.warning('Feishu push exception: %s', e)
        return False


# ── IPO 扫描器 ───────────────────────────────────────────────────────────────


class IPOScanner:
    """
    港股新股扫描器。

    功能：
      1. 每日 09:00 扫描港交所新股列表（东方财富）
      2. 幂等：对比 IPORecordStore，已分析过的跳过
      3. 新股 → 全量分析（多源获取→交叉验证→分析引擎→飞书推送）
      4. 跟踪招股状态变化（聆讯→申购→上市）

    典型用法：
      scanner = IPOScanner()
      summary = scanner.scan()          # cron job 调用
      result  = scanner.analyze_one('09619')  # API 手动触发
    """

    def __init__(self):
        from core.ipo_data_source import IPODataSource
        from core.ipo_cross_validator import DataCrossValidator
        from core.ipo_analyst_engine import IPOAnalystEngine
        from core.ipo_analyst_engine import IPORecordStore

        self.data_source = IPODataSource()
        self.validator = DataCrossValidator()
        self.analyst = IPOAnalystEngine()
        self.store = IPORecordStore()

    # ── 主扫描流程 ───────────────────────────────────────────────────────────

    def scan(self) -> dict:
        """
        主扫描流程，供 cron job 调用。

        步骤：
          1. 获取东方财富招股中列表（申购中+待上市）
          2. 过滤掉已在 IPORecordStore 中有完整分析记录的
          3. 对每只新股：
             - get_all_sources()  多源数据获取
             - cross_validate()   交叉验证
             - merge_with_confidence() → validated_data
             - analyst.analyze()  完整分析
             - _render_and_send() 飞书推送
             - store 保存记录
          4. 收集错误，单只失败不影响其他

        返回：
          dict：
            {
                'scanned_at': str,          # ISO 时间戳
                'total_found': int,         # 本次扫描到的总数
                'new_count': int,           # 新分析的数量
                'skipped_count': int,       # 跳过的数量
                'error_count': int,         # 出错的数量
                'new_ipos': List[{'stock_code', 'name_cn', 'listing_date'}],
                'errors': List[str],
            }
        """
        _log.info('IPO scan started at %s', datetime.now().isoformat())

        # Step 1: 获取即将上市新股列表
        upcoming: List = []
        try:
            upcoming = self.data_source.get_upcoming_ipos(force_refresh=False)
            _log.info('Found %d upcoming IPOs', len(upcoming))
        except Exception as e:
            _log.error('Failed to fetch upcoming IPOs: %s', e)
            return self._empty_result(str(e))

        new_ipos: List[Dict] = []
        skipped_count = 0
        error_count = 0
        errors: List[str] = []

        # Step 2: 逐只处理
        for ipo in upcoming:
            stock_code = getattr(ipo, 'stock_code', None) or getattr(ipo, 'stock_code', '')
            if not stock_code:
                continue

            # 幂等检查
            if self._should_skip(stock_code):
                _log.debug('Skipping %s (already analyzed)', stock_code)
                skipped_count += 1
                continue

            # Step 3: 完整分析流程
            _log.info('Analyzing new IPO: %s', stock_code)
            result = self._analyze_and_report(stock_code)

            if result['success']:
                new_ipos.append({
                    'stock_code': stock_code,
                    'name_cn': result.get('name_cn', ''),
                    'listing_date': result.get('listing_date', ''),
                })
            else:
                error_count += 1
                errors.append(f'[{stock_code}] {result.get("error", "unknown error")}')

        # Step 4: 汇总
        summary = {
            'scanned_at': datetime.now().isoformat(),
            'total_found': len(upcoming),
            'new_count': len(new_ipos),
            'skipped_count': skipped_count,
            'error_count': error_count,
            'new_ipos': new_ipos,
            'errors': errors,
        }

        _log.info(
            'IPO scan completed: total=%d new=%d skipped=%d errors=%d',
            len(upcoming), len(new_ipos), skipped_count, error_count,
        )
        return summary

    # ── 单只手动分析 ─────────────────────────────────────────────────────────

    def analyze_one(self, stock_code: str) -> dict:
        """
        手动分析单只股票（供 API 调用）。

        流程同 scan()，但不检查已有记录，强制重新分析。

        参数：
          stock_code : str   港股股票代码（5位，如 '09619'）

        返回：
          dict：
            {
                'stock_code': str,
                'success': bool,
                'report': IPOAnalysisReport or None,
                'error': str or None,
            }
        """
        _log.info('analyze_one: %s (force)', stock_code)
        result = self._analyze_and_report(stock_code)
        result['stock_code'] = stock_code
        return result

    # ── 内部方法 ─────────────────────────────────────────────────────────────

    def _should_skip(self, stock_code: str) -> bool:
        """
        检查是否已分析过（IPOStore 中有完整分析记录）。

        判断逻辑：遍历所有记录，stock_code 匹配且有以下关键字段
        即视为已完整分析：
          - name_cn / name（股票名称）
          - listing_date（上市日期）
          - overall_rating（综合评级）
        """
        try:
            records = self.store.get_all_records()
            for rec in records:
                rec_code = rec.get('stock_code', '')
                # 兼容字段名
                code_match = (
                    rec_code == stock_code
                    or rec_code.zfill(5) == stock_code.zfill(5)
                    or stock_code.zfill(5) == rec_code.zfill(5)
                )
                if code_match:
                    # 检查是否完整（有评级字段即视为已分析）
                    if rec.get('overall_rating') or rec.get('rating'):
                        _log.debug(
                            '_should_skip(%s): found existing record, skipping',
                            stock_code,
                        )
                        return True
        except Exception as e:
            _log.warning('_should_skip(%s) check failed: %s', stock_code, e)
        return False

    def _analyze_and_report(self, stock_code: str) -> dict:
        """
        执行单只股票的完整分析流程。

        步骤：
          1. get_all_sources() → 多源原始数据
          2. 构建 cross_validate 所需格式
          3. cross_validate() → CrossValidationReport
          4. merge_with_confidence() → validated_data
          5. analyst.analyze() → IPOAnalysisReport
          6. _render_and_send() → 飞书推送
          7. store.add_record() → 持久化

        返回：
          dict：{'success', 'report', 'name_cn', 'listing_date', 'error'}
        """
        try:
            # ── Step 1: 多源数据获取 ─────────────────────────────────────
            multi_source = self.data_source.get_all_sources(stock_code)

            # ── Step 2: 构建交叉验证格式 ───────────────────────────────
            # DataCrossValidator.cross_validate 期望 eastmoney/hkexnews/news 分离
            ipo_info = multi_source.get('ipo_info')
            hkex_info = multi_source.get('hkex', {})

            cv_format: Dict[str, Any] = {
                'stock_code': stock_code,
                'eastmoney': {},
                'hkexnews': {},
                'news': {},
            }

            if ipo_info is not None:
                cv_format['eastmoney'] = {
                    'issue_price_range': (
                        getattr(ipo_info, 'issue_price_low', None),
                        getattr(ipo_info, 'issue_price_high', None),
                    ),
                    'industry': getattr(ipo_info, 'industry', None),
                    'sponsor': getattr(ipo_info, 'sponsor', None),
                    'fund_raised': getattr(ipo_info, 'proceeds', None),
                    'cornerstone_investors': getattr(ipo_info, 'cornerstone_investors', []),
                }

            if hkex_info:
                cv_format['hkexnews'] = {
                    'listing_status': hkex_info.get('listing_status'),
                    'cornerstone_investors': hkex_info.get('cornerstone_investors', []),
                }

            # ── Step 3: 交叉验证 ─────────────────────────────────────────
            validation = self.validator.cross_validate(stock_code, cv_format)

            # ── Step 4: 合并验证结果 ────────────────────────────────────
            validated = self.validator.merge_with_confidence(cv_format, validation)

            # ── Step 5: 分析引擎 ────────────────────────────────────────
            report = self.analyst.analyze(
                stock_code=stock_code,
                multi_source_data=multi_source,
                validated_data=validated,
                market_sentiment=None,  # 可扩展：注入市场情绪数据
            )

            # ── Step 6: 飞书推送 ────────────────────────────────────────
            sent = self._render_and_send(report)
            if not sent:
                _log.warning('Failed to send Feishu report for %s', stock_code)

            # ── Step 7: 持久化 ──────────────────────────────────────────
            self._save_record(report)

            name_cn = getattr(ipo_info, 'stock_name', None) or report.name_cn
            listing_date = (
                getattr(ipo_info, 'listing_date', None)
                or (report.listing_date.isoformat() if report.listing_date else '')
            )

            return {
                'success': True,
                'report': report,
                'name_cn': name_cn,
                'listing_date': listing_date,
                'error': None,
            }

        except Exception as e:
            _log.error('Failed to analyze %s: %s', stock_code, e, exc_info=True)
            return {
                'success': False,
                'report': None,
                'name_cn': '',
                'listing_date': '',
                'error': str(e),
            }

    def _render_and_send(self, report) -> bool:
        """
        将 IPOAnalysisReport 渲染为飞书文本并发送。

        格式：Markdown 风格的简洁报告，包含：
          - 股票代码、名称、上市日期
          - 综合评级（BUY/NEUTRAL/SKIP）+ 置信度
          - 首日预测涨幅（p25/p50/p75）
          - 核心信号（基石投资者、条款评分、市场情绪）
          - 风险提示
          - 限价单建议（暗盘 + 首日）

        参数：
          report : IPOAnalysisReport

        返回：
          bool：发送是否成功
        """
        try:
            # 评级 emoji
            rating_emoji = {
                'BUY': '🟢 BUY',
                'NEUTRAL': '🟡 NEUTRAL',
                'SKIP': '🔴 SKIP',
            }
            rating_text = rating_emoji.get(report.overall_rating, report.overall_rating)

            # 预测涨幅格式化
            p25 = report.predicted_first_day_return_p25
            p50 = report.predicted_first_day_return_p50
            p75 = report.predicted_first_day_return_p75

            lines = [
                f"📈 **{report.name_cn}（{report.stock_code}）** 上市日 {report.listing_date}",
                f"─────────────────────────────────────",
                f"【综合评级】{rating_text}  置信度 {report.confidence:.0%}",
                f"【首日预测涨幅】保守 {p25*100:+.1f}%  /  中性 {p50*100:+.1f}%  /  乐观 {p75*100:+.1f}%",
                f"【发行条款评分】{report.terms_score:.2f}  |  【机构持仓评分】{report.institutional_score:.2f}",
                f"【市场情绪】{report.market_sentiment_score:.2f}  |  【打新胜率】{report.recent_ipo_win_rate:.0%}",
            ]

            # 基石投资者
            if report.cornerstone_signals:
                lines.append(f"【基石信号】{' | '.join(report.cornerstone_signals[:3])}")

            # 利好信号
            if report.key_positive_signals:
                pos = report.key_positive_signals[:2]
                lines.append(f"【利好】{' | '.join(pos)}")

            # 风险提示
            if report.risk_factors:
                risks = report.risk_factors[:2]
                lines.append(f"【风险】{' | '.join(risks)}")

            # 限价单建议
            if report.dark_pool_recommendation:
                dp = report.dark_pool_recommendation
                lines.append(
                    f"【暗盘限价单】保守 {dp.conservative_price:.2f}  |  "
                    f"中性 {dp.neutral_price:.2f}  |  进取 {dp.aggressive_price:.2f}"
                )
            if report.first_day_recommendation:
                fd = report.first_day_recommendation
                lines.append(
                    f"【首日限价单】保守 {fd.conservative_price:.2f}  |  "
                    f"中性 {fd.neutral_price:.2f}  |  进取 {fd.aggressive_price:.2f}"
                )

            # 数据质量
            overall_conf = report.data_quality_score.get('overall', 0.0)
            lines.append(f"─────────────────────────────────────")
            lines.append(f"数据质量 {overall_conf:.0%}  |  生成时间 {report.generated_at.strftime('%H:%M:%S')}")

            text = '\n'.join(lines)
            return _feishu_push(text)

        except Exception as e:
            _log.error('Failed to render/send report: %s', e)
            return False

    def _save_record(self, report) -> None:
        """
        将分析报告保存到 IPORecordStore。

        保存字段：
          - stock_code, name, listing_date
          - issue_price, first_day_return（预测值）
          - overall_rating, confidence
          - industry, fund_raised_hkd
          - cornerstone_investors（序列化）
          - raw report dict（JSON 序列化）
        """
        try:
            record: Dict[str, Any] = {
                'stock_code': report.stock_code,
                'name': report.name_cn,
                'name_en': report.name_en,
                'listing_date': (
                    report.listing_date.isoformat()
                    if hasattr(report.listing_date, 'isoformat')
                    else str(report.listing_date)
                ),
                'issue_price': report.mid_price,
                'issue_price_low': report.issue_price_range[0],
                'issue_price_high': report.issue_price_range[1],
                'industry': '',  # 可从 validated_data 补充
                'fund_raised_hkd': 0.0,  # 可从 validated_data 补充
                'first_day_return': report.predicted_first_day_return_p50,
                'grey_market_return': 0.0,
                'overall_rating': report.overall_rating,
                'confidence': report.confidence,
                'predicted_p25': report.predicted_first_day_return_p25,
                'predicted_p50': report.predicted_first_day_return_p50,
                'predicted_p75': report.predicted_first_day_return_p75,
                'terms_score': report.terms_score,
                'institutional_score': report.institutional_score,
                'market_sentiment_score': report.market_sentiment_score,
                'cornerstone_investors': ','.join(report.cornerstone_signals),
                'risk_factors': '|'.join(report.risk_factors),
                'key_positive_signals': '|'.join(report.key_positive_signals),
                'analysed_at': datetime.now().isoformat(),
                # 完整报告 JSON
                '_report_json': json.dumps(
                    report.to_dict(),
                    ensure_ascii=False,
                    default=str,
                ),
            }
            self.store.add_record(record)
            _log.info('_save_record: saved %s', report.stock_code)
        except Exception as e:
            _log.error('Failed to save record for %s: %s', report.stock_code, e)

    @staticmethod
    def _empty_result(error: str = '') -> dict:
        """返回空结果（扫描完全失败时使用）。"""
        return {
            'scanned_at': datetime.now().isoformat(),
            'total_found': 0,
            'new_count': 0,
            'skipped_count': 0,
            'error_count': 0,
            'new_ipos': [],
            'errors': [error] if error else [],
        }


# ── CLI 入口 ─────────────────────────────────────────────────────────────────

def main():
    """供命令行和 cron job 调用。"""
    import argparse

    parser = argparse.ArgumentParser(description='IPO Scanner')
    parser.add_argument(
        '--code', type=str, default='',
        help='Single stock code to analyze (skip existing check)',
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(levelname)s %(message)s',
    )

    scanner = IPOScanner()

    if args.code:
        result = scanner.analyze_one(args.code)
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    else:
        summary = scanner.scan()
        print(json.dumps(summary, ensure_ascii=False, default=str, indent=2))


if __name__ == '__main__':
    main()

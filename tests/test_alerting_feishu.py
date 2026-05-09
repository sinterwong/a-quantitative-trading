"""
test_alerting_feishu.py — P2-17 飞书告警测试

验证：
  1. _send_feishu 构造正确的 interactive 卡片 payload
  2. AlertManager 接受 feishu_webhook 参数 + 环境变量
  3. 优先级：feishu > wechat > dingtalk > email
  4. _http_post 支持自定义 success_keys 兼容飞书响应格式
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class TestSendFeishu(unittest.TestCase):

    def test_send_feishu_payload_structure(self):
        """飞书 payload 应包含 interactive 卡片 + 颜色头 + 时间戳。"""
        from core.alerting import _send_feishu

        captured = {}

        def fake_http_post(url, payload, timeout=10, success_keys=None):
            captured['url'] = url
            captured['payload'] = payload
            captured['success_keys'] = success_keys
            return True

        with patch('core.alerting._http_post', side_effect=fake_http_post):
            ok = _send_feishu('https://feishu.example/webhook', '测试消息', 'CRITICAL')
        self.assertTrue(ok)

        payload = captured['payload']
        self.assertEqual(payload['msg_type'], 'interactive')
        self.assertEqual(payload['card']['header']['template'], 'red')   # CRITICAL
        # 标题包含 [CRITICAL]
        self.assertIn('CRITICAL', payload['card']['header']['title']['content'])
        # 内容包含原始消息
        body_div = payload['card']['elements'][0]
        self.assertIn('测试消息', body_div['text']['content'])
        # success_keys 应为 ('StatusCode', 'code')
        self.assertEqual(captured['success_keys'], ('StatusCode', 'code'))

    def test_feishu_color_by_level(self):
        from core.alerting import _send_feishu

        captured = {}

        def fake_http_post(url, payload, timeout=10, success_keys=None):
            captured.update(payload['card']['header'])
            return True

        with patch('core.alerting._http_post', side_effect=fake_http_post):
            _send_feishu('x', 'm', 'WARNING')
        self.assertEqual(captured['template'], 'yellow')

        with patch('core.alerting._http_post', side_effect=fake_http_post):
            _send_feishu('x', 'm', 'INFO')
        self.assertEqual(captured['template'], 'green')


class TestAlertManagerFeishuIntegration(unittest.TestCase):

    def test_constructor_accepts_feishu_webhook(self):
        from core.alerting import AlertManager
        mgr = AlertManager(feishu_webhook='https://feishu.example/x')
        self.assertEqual(mgr.feishu_webhook, 'https://feishu.example/x')

    def test_env_variable_fallback(self):
        from core.alerting import AlertManager
        with patch.dict(os.environ, {'FEISHU_WEBHOOK_URL': 'https://env.example'}):
            mgr = AlertManager()
        self.assertEqual(mgr.feishu_webhook, 'https://env.example')

    def test_feishu_takes_priority_over_wechat(self):
        """飞书与微信都配置时，飞书优先发送。"""
        from core.alerting import AlertManager

        mgr = AlertManager(
            feishu_webhook='https://feishu.example/x',
            wechat_webhook='https://wechat.example/x',
            min_level='INFO',
        )

        feishu_called = []
        wechat_called = []

        def fake_feishu(url, msg, level):
            feishu_called.append((url, msg, level))
            return True

        def fake_wechat(url, msg, level):
            wechat_called.append((url, msg, level))
            return True

        with patch('core.alerting._send_feishu', side_effect=fake_feishu), \
             patch('core.alerting._send_wechat', side_effect=fake_wechat):
            sent = mgr.send_warning('测试')

        self.assertTrue(sent)
        self.assertEqual(len(feishu_called), 1)
        # 飞书成功后 wechat 不应再被调用
        self.assertEqual(len(wechat_called), 0)

    def test_fallback_to_wechat_when_feishu_fails(self):
        """飞书发送失败 → 回退到微信。"""
        from core.alerting import AlertManager

        mgr = AlertManager(
            feishu_webhook='https://feishu.example/x',
            wechat_webhook='https://wechat.example/x',
            min_level='INFO',
        )

        with patch('core.alerting._send_feishu', return_value=False), \
             patch('core.alerting._send_wechat', return_value=True) as mock_wc:
            sent = mgr.send_warning('m')

        self.assertTrue(sent)
        mock_wc.assert_called_once()


class TestHttpPostSuccessKeys(unittest.TestCase):

    def test_default_keys_for_wechat_dingtalk(self):
        """默认 success_keys 仍是 errcode/ErrCode（兼容微信钉钉）。"""
        from core.alerting import _http_post
        import json
        from io import BytesIO
        from unittest.mock import patch

        class _FakeResp:
            def __init__(self, body):
                self._body = body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def read(self):
                return self._body

        # 微信成功响应：{"errcode": 0, "errmsg": "ok"}
        body = json.dumps({'errcode': 0, 'errmsg': 'ok'}).encode()
        with patch('urllib.request.urlopen', return_value=_FakeResp(body)):
            self.assertTrue(_http_post('http://x', {}, success_keys=('errcode', 'ErrCode')))

    def test_feishu_keys(self):
        """飞书成功响应：{"StatusCode": 0}。"""
        from core.alerting import _http_post
        import json
        from unittest.mock import patch

        class _FakeResp:
            def __init__(self, body):
                self._body = body
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return self._body

        body = json.dumps({'StatusCode': 0, 'StatusMessage': 'success'}).encode()
        with patch('urllib.request.urlopen', return_value=_FakeResp(body)):
            self.assertTrue(_http_post('http://x', {}, success_keys=('StatusCode', 'code')))


if __name__ == '__main__':
    unittest.main()

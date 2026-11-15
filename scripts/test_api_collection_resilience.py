"""Authentication failure isolation and rotating refresh-token regression tests."""
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import fetch_api_sales as api
from scripts.common import APIHTTPError


class CollectionResilience(unittest.TestCase):
    def test_401_refresh_retries_and_persists_rotated_tokens(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            cache = Path(directory) / 'tokens.json'
            session = MagicMock()
            session.post.return_value.ok = True
            session.post.return_value.json.return_value = {'access_token': 'new-access', 'refresh_token': 'rotated-refresh'}
            env = {'CAFE24_MALL_ID': 'testmall', 'CAFE24_ACCESS_TOKEN': 'old-access',
                   'CAFE24_REFRESH_TOKEN': 'old-refresh', 'CAFE24_CLIENT_ID': 'test-client',
                   'CAFE24_CLIENT_SECRET': 'test-secret'}
            authorization = []
            def request(*args, **kwargs):
                authorization.append(kwargs['headers']['Authorization'])
                if len(authorization) == 1:
                    raise APIHTTPError(401)
                return {'orders': []}
            with patch.dict(os.environ, env), patch.object(api, 'TOKEN_CACHE', cache), \
                    patch.object(api, 'request_json', side_effect=request):
                self.assertEqual(api.fetch_cafe24(date(2026, 11, 15), session), ([{'orders': []}], []))
                self.assertEqual(api.cafe24_tokens('testmall')['refresh_token'], 'rotated-refresh')
            self.assertEqual(authorization, ['Bearer old-access', 'Bearer new-access'])
            session.post.assert_called_once()

    def test_refresh_then_second_401_is_not_retried_forever(self):
        with patch.dict(os.environ, {'CAFE24_MALL_ID': 'testmall', 'CAFE24_ACCESS_TOKEN': 'old'}), \
                patch.object(api, 'cafe24_tokens', return_value={'access_token': 'old', 'refresh_token': 'refresh'}), \
                patch.object(api, 'refresh_cafe24', return_value='new') as refresh, \
                patch.object(api, 'request_json', side_effect=APIHTTPError(401)) as request:
            with self.assertRaises(APIHTTPError):
                api.fetch_cafe24(date(2026, 11, 15), MagicMock())
            refresh.assert_called_once()
            self.assertEqual(request.call_count, 2)

    def test_channel_failure_keeps_files_and_continues_other_channel(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            folder = root / 'raw_data/sales/cafe24'
            folder.mkdir(parents=True)
            old = folder / '2026-11-15.csv'
            old.write_bytes(b'old data must survive')
            with patch.object(api, 'ROOT', root), patch.object(api, 'load_env'), \
                    patch.object(api, 'fetch_cafe24', side_effect=APIHTTPError(401)), \
                    patch.object(api, 'fetch_smartstore', return_value=([], [])) as smart:
                with self.assertLogs(api.LOG, level='WARNING') as logs:
                    result = api.run(date(2026, 11, 15))
            self.assertEqual(old.read_bytes(), b'old data must survive')
            self.assertEqual(result['cafe24']['status'], 'fallback')
            self.assertEqual(result['smartstore'], 0)
            smart.assert_called_once()
            self.assertIn('cafe24', logs.output[0])
            self.assertIn('401', logs.output[0])

    def test_refresh_401_does_not_log_tokens(self):
        session = MagicMock()
        session.post.return_value.ok = False
        session.post.return_value.status_code = 401
        with patch.dict(os.environ, {'CAFE24_CLIENT_ID': 'test', 'CAFE24_CLIENT_SECRET': 'private-secret'}):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 401') as error:
                api.refresh_cafe24(session, 'test', {'refresh_token': 'private-refresh'})
        self.assertNotIn('private-secret', str(error.exception))
        self.assertNotIn('private-refresh', str(error.exception))

    def test_smartstore_401_identifies_order_endpoint(self):
        with patch.object(api, 'naver_token', return_value='token'), \
                patch.object(api, 'request_json', side_effect=APIHTTPError(401)):
            with self.assertRaisesRegex(RuntimeError, '스마트스토어 주문조회 API HTTP 401'):
                api.fetch_smartstore(date(2026, 11, 15), MagicMock())

    def test_missing_refresh_token_is_explicit(self):
        with self.assertRaisesRegex(RuntimeError, 'HTTP 401.*CAFE24_REFRESH_TOKEN'):
            api.refresh_cafe24(MagicMock(), 'test', {'refresh_token': ''})


if __name__ == '__main__':
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from django.conf import settings
from django.test import override_settings
from app.services.ai_client import CachedResponses


class ResponseCacheTests(unittest.TestCase):
    def test_concurrent_requests_pay_once(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        import time
        gate = threading.Barrier(8)
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=str(Path(directory)/'uploads')):
            client = Mock()
            def respond(**kwargs):
                time.sleep(0.03)
                return SimpleNamespace(status='completed', output_text='{}', usage=None)
            client.responses.create.side_effect = respond
            def invoke(_):
                gate.wait(timeout=5)
                return CachedResponses(client, 'tenant').create(input='same').output_text
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(list(pool.map(invoke, range(8))), ['{}'] * 8)
            self.assertEqual(client.responses.create.call_count, 1)

    def test_success_reused_and_changed_input_invalidates(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=str(Path(directory)/'uploads')):
            client = Mock()
            client.responses.create.return_value = SimpleNamespace(status='completed', output_text='{"number":"240"}', usage=None)
            cached = CachedResponses(client, 'tenant')
            self.assertEqual(cached.create(model='test', input='one').output_text, '{"number":"240"}')
            cached.create(model='test', input='one')
            self.assertEqual(client.responses.create.call_count, 1)
            cached.create(model='test', input='two')
            self.assertEqual(client.responses.create.call_count, 2)
            CachedResponses(client, 'other').create(model='test', input='one')
            self.assertEqual(client.responses.create.call_count, 3)

    def test_missing_required_fields_are_not_cached(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=str(Path(directory)/'uploads')):
            client = Mock()
            client.responses.create.return_value = SimpleNamespace(status='completed', output_text='{}', usage=None)
            cached = CachedResponses(client, 'tenant')
            request = {'text': {'format': {'schema': {'required': ['number'], 'properties': {'number': {'type': 'string'}, 'optional': {'type': 'string', 'enum': []}}}}}}
            cached.create(**request)
            cached.create(**request)
            self.assertEqual(client.responses.create.call_count, 2)

    def test_incomplete_and_invalid_json_are_not_cached(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=str(Path(directory)/'uploads')):
            client = Mock()
            cached = CachedResponses(client, 'tenant')
            for status, output in [('incomplete', ''), ('completed', 'invalid')]:
                client.responses.create.return_value = SimpleNamespace(status=status, output_text=output, usage=None)
                cached.create(input='same')
                cached.create(input='same')
            self.assertEqual(client.responses.create.call_count, 4)


if __name__ == '__main__':
    if not settings.configured:
        settings.configure()
    unittest.main()

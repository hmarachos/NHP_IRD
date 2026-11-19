"""Cache successful structured responses and record token usage outside the database."""
import fcntl
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from django.conf import settings

logger = logging.getLogger(__name__)


def _valid_output(output, request):
    data = json.loads(output)
    schema = request.get('text', {}).get('format', {}).get('schema')
    if schema:
        if not isinstance(data, dict) or any(key not in data for key in schema.get('required', [])):
            raise ValueError('Incomplete structured response')
        for key, definition in schema.get('properties', {}).items():
            if key in data:
                kind = definition.get('type')
                if kind == 'string' and not isinstance(data[key], str):
                    raise ValueError('Invalid string field')
                if kind == 'boolean' and not isinstance(data[key], bool):
                    raise ValueError('Invalid boolean field')
    return data


class CachedResponses:
    def __init__(self, client, namespace):
        self.client = client
        self.namespace = namespace

    def create(self, **request):
        root = Path(settings.MEDIA_ROOT).parent / 'ai-response-cache'
        digest = hashlib.sha256((self.namespace + json.dumps(request, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = (root / (digest + '.lock')).open('a')
        except OSError:
            logger.warning('AI cache lock unavailable', exc_info=True)
            return self._create(**request)
        # Shared by web threads, worker processes and containers on this volume.
        with lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                return self._create(**request)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _create(self, **request):
        root = Path(settings.MEDIA_ROOT).parent / 'ai-response-cache'
        # Separate credentials without ever persisting the credential itself.
        digest = hashlib.sha256((self.namespace + json.dumps(request, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
        path = root / (digest + '.json')
        try:
            data = json.loads(path.read_text())
            _valid_output(data['output_text'], request)
            logger.info('AI cache hit model=%s', request.get('model'))
            return SimpleNamespace(output_text=data['output_text'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        response = self.client.responses.create(**request)
        usage = response.usage.model_dump() if getattr(response, 'usage', None) else {}
        logger.info('AI usage model=%s usage=%s', request.get('model'), json.dumps(usage))
        if getattr(response, 'status', None) != 'completed' or not response.output_text:
            return response
        try:
            _valid_output(response.output_text, request)
        except ValueError:
            return response
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode='w', dir=root, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({'output_text': response.output_text, 'usage': usage}, stream, ensure_ascii=False)
            os.replace(temporary, path)
        except OSError:
            logger.warning('Cannot persist AI response cache', exc_info=True)
        return response


def ai_client(api_key):
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    return SimpleNamespace(responses=CachedResponses(client, hashlib.sha256(api_key.encode()).hexdigest()))

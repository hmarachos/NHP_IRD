"""Cache successful structured responses and record token usage outside the database."""
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from django.conf import settings

logger = logging.getLogger(__name__)


class CachedResponses:
    def __init__(self, client, namespace):
        self.client = client
        self.namespace = namespace

    def create(self, **request):
        root = Path(settings.MEDIA_ROOT).parent / 'ai-response-cache'
        # Separate credentials without ever persisting the credential itself.
        digest = hashlib.sha256((self.namespace + json.dumps(request, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
        path = root / (digest + '.json')
        try:
            data = json.loads(path.read_text())
            json.loads(data['output_text'])
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
            json.loads(response.output_text)
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

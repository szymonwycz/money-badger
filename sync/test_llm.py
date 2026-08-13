"""Checks for the LLM abstraction and the rules-only path.

No network: every OpenAI-compatible backend — the hosted ones included, they are
the same shape at a different URL — is exercised against a local HTTP server, and
the Anthropic one is never called here.
"""
import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import llm
import main


def _clear_env():
    for k in ('LLM_PROVIDER', 'LLM_BASE_URL', 'LLM_MODEL', 'LLM_API_KEY',
              'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY',
              'MOONSHOT_API_KEY', 'GEMINI_API_KEY', 'XAI_API_KEY'):
        os.environ.pop(k, None)
    llm._substitutes.clear()


def test_not_configured_when_nothing_is_set():
    _clear_env()
    assert llm.configured() is False


def test_anthropic_needs_a_key():
    _clear_env()
    os.environ['LLM_PROVIDER'] = 'anthropic'
    assert llm.configured() is False
    os.environ['ANTHROPIC_API_KEY'] = 'sk-ant-test'
    assert llm.configured() is True
    _clear_env()


def test_openai_backend_needs_a_base_url_or_a_key():
    _clear_env()
    os.environ['LLM_PROVIDER'] = 'openai'
    assert llm.configured() is False
    os.environ['LLM_BASE_URL'] = 'http://localhost:1/v1'
    assert llm.configured() is True
    _clear_env()


def test_unknown_provider_says_what_to_use():
    _clear_env()
    os.environ['LLM_PROVIDER'] = 'gpt5000'
    os.environ['LLM_MODEL'] = 'whatever'
    with pytest.raises(RuntimeError, match='deepseek'):
        llm.complete('hi')
    _clear_env()


def test_extract_json_survives_fences_and_chatter():
    """Local models wrap answers in prose far more often than the hosted ones."""
    assert llm.extract_json('[{"a": 1}]') == [{'a': 1}]
    assert llm.extract_json('```json\n[{"a": 1}]\n```') == [{'a': 1}]
    assert llm.extract_json('Sure! Here you go:\n[{"a": 1}]\nHope that helps.') == [{'a': 1}]
    assert llm.extract_json('{"k": "v"}') == {'k': 'v'}


class _Stub(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint: /chat/completions and /models."""

    models = ()
    retired = ()
    list_broken = False
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self._record(body)
        if body['model'] in _Stub.retired:
            return self._reply(404, {'error': {
                'code': 'model_not_found',
                'message': f"The model `{body['model']}` does not exist"}})
        if body['model'].startswith('gpt-5') and 'max_tokens' in body:
            return self._reply(400, {'error': {'message':
                "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'."}})
        self._reply(200, {'choices': [{'message': {
            'content': '[{"idx": 0, "category": "Groceries"}]'}}]})

    def do_GET(self):
        self._record(None)
        if _Stub.list_broken:
            return self._reply(500, {'error': {'message': 'upstream on fire'}})
        self._reply(200, {'data': [{'id': m} for m in _Stub.models]})

    def _record(self, body):
        _Stub.requests.append({'path': self.path, 'body': body,
                               'auth': self.headers.get('Authorization')})

    def _reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@contextmanager
def _stub(models=(), retired=(), list_broken=False):
    _Stub.models, _Stub.retired, _Stub.requests = models, retired, []
    _Stub.list_broken = list_broken
    server = HTTPServer(('127.0.0.1', 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _clear_env()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1'
    finally:
        server.shutdown()
        _clear_env()


def test_openai_backend_speaks_the_chat_completions_shape():
    with _stub() as base:
        os.environ['LLM_PROVIDER'] = 'openai'
        os.environ['LLM_BASE_URL'] = base
        os.environ['LLM_MODEL'] = 'qwen3:30b'
        out = llm.complete('categorize this', task='categorize', system='be terse')
        assert llm.extract_json(out) == [{'idx': 0, 'category': 'Groceries'}]
        sent = _Stub.requests[-1]
        assert sent['path'] == '/v1/chat/completions'
        assert sent['auth'] is None       # a local server usually wants no key
        assert sent['body']['model'] == 'qwen3:30b'
        assert sent['body']['messages'][0] == {'role': 'system', 'content': 'be terse'}
        assert sent['body']['messages'][1]['content'] == 'categorize this'


def test_a_hosted_provider_is_a_base_url_and_a_key(monkeypatch):
    """DeepSeek, Kimi, Gemini and Grok differ from OpenAI only in those two."""
    with _stub() as base:
        monkeypatch.setitem(llm.PROVIDERS, 'deepseek', (base, 'DEEPSEEK_API_KEY'))
        os.environ['LLM_PROVIDER'] = 'deepseek'
        os.environ['DEEPSEEK_API_KEY'] = 'sk-deep'
        os.environ['LLM_MODEL'] = 'deepseek-chat'
        assert llm.configured() is True   # no LLM_BASE_URL needed
        assert llm.extract_json(llm.complete('categorize this')) == [
            {'idx': 0, 'category': 'Groceries'}]
        sent = _Stub.requests[-1]
        assert sent['auth'] == 'Bearer sk-deep'
        assert sent['body']['model'] == 'deepseek-chat'


def test_newer_openai_models_get_max_completion_tokens(monkeypatch):
    with _stub() as base:
        monkeypatch.setitem(llm.PROVIDERS, 'openai', (base, 'OPENAI_API_KEY'))
        os.environ['LLM_PROVIDER'] = 'openai'
        os.environ['OPENAI_API_KEY'] = 'sk-test'
        os.environ['LLM_MODEL'] = 'gpt-5-mini'
        assert llm.extract_json(llm.complete('categorize this'))
        assert 'max_tokens' not in _Stub.requests[-1]['body']
        assert _Stub.requests[-1]['body']['max_completion_tokens'] == 4000


def test_list_models_reads_the_list_and_strips_geminis_prefix(monkeypatch):
    with _stub(models=('gpt-4o', 'models/gemini-3-flash', 'deepseek-chat')) as base:
        monkeypatch.setitem(llm.PROVIDERS, 'gemini', (base, 'GEMINI_API_KEY'))
        os.environ['LLM_PROVIDER'] = 'gemini'
        os.environ['GEMINI_API_KEY'] = 'g-key'
        assert llm.list_models() == ['deepseek-chat', 'gemini-3-flash', 'gpt-4o']
        assert _Stub.requests[-1]['path'] == '/v1/models'
        assert _Stub.requests[-1]['auth'] == 'Bearer g-key'


def test_a_retired_model_is_replaced_by_its_nearest_sibling(monkeypatch, capsys):
    """Never silently: a sync that changed model says which, and how to pin it."""
    with _stub(models=('deepseek-chat-v4', 'deepseek-reasoner', 'gpt-4o'),
               retired=('deepseek-chat',)) as base:
        monkeypatch.setitem(llm.PROVIDERS, 'deepseek', (base, 'DEEPSEEK_API_KEY'))
        os.environ['LLM_PROVIDER'] = 'deepseek'
        os.environ['DEEPSEEK_API_KEY'] = 'sk-deep'
        os.environ['LLM_MODEL'] = 'deepseek-chat'
        assert llm.extract_json(llm.complete('categorize this')) == [
            {'idx': 0, 'category': 'Groceries'}]
        assert _Stub.requests[-1]['body']['model'] == 'deepseek-chat-v4'
        logged = capsys.readouterr().out
        assert 'deepseek-chat' in logged and 'LLM_MODEL=deepseek-chat-v4' in logged
        # Repaired once, not once per batch: the next call goes straight there.
        llm.complete('and this')
        asked = [r['body']['model'] for r in _Stub.requests if r['body']]
        assert asked.count('deepseek-chat') == 1
        assert [r['path'] for r in _Stub.requests].count('/v1/models') == 1


def test_no_sibling_model_fails_loudly_with_the_alternatives(monkeypatch):
    with _stub(models=('gpt-4o', 'gpt-4o-mini'), retired=('deepseek-chat',)) as base:
        monkeypatch.setitem(llm.PROVIDERS, 'deepseek', (base, 'DEEPSEEK_API_KEY'))
        os.environ['LLM_PROVIDER'] = 'deepseek'
        os.environ['DEEPSEEK_API_KEY'] = 'sk-deep'
        os.environ['LLM_MODEL'] = 'deepseek-chat'
        with pytest.raises(RuntimeError, match='LLM_MODEL') as exc:
            llm.complete('categorize this')
        assert 'gpt-4o' in str(exc.value)


def test_a_model_missing_without_a_usable_list_says_what_to_set(monkeypatch):
    with _stub(retired=('deepseek-chat',), list_broken=True) as base:
        monkeypatch.setitem(llm.PROVIDERS, 'deepseek', (base, 'DEEPSEEK_API_KEY'))
        os.environ['LLM_PROVIDER'] = 'deepseek'
        os.environ['DEEPSEEK_API_KEY'] = 'sk-deep'
        os.environ['LLM_MODEL'] = 'deepseek-chat'
        with pytest.raises(RuntimeError, match='Set LLM_MODEL in .env'):
            llm.complete('categorize this')


def test_hosted_provider_without_a_model_says_where_to_look():
    _clear_env()
    os.environ['LLM_PROVIDER'] = 'xai'
    os.environ['XAI_API_KEY'] = 'x-key'
    with pytest.raises(RuntimeError, match='--models'):
        llm.complete('categorize this')
    _clear_env()


def test_categorization_falls_back_to_check_me_without_an_llm(monkeypatch):
    """The bank-only variant: rules handle what they can, the rest is reviewable."""
    _clear_env()
    # monkeypatch so this module state doesn't leak into the other sync tests.
    monkeypatch.setattr(main, '_categories', ('EXPENSES:\n  Groceries\n', {'Groceries', 'CHECK ME'}))
    cfg = {'own_ibans': [], 'batch_size': 30}
    txs = [
        {'title': 'LIDL 123', 'counterpart': '', 'amount': -50.0, 'transaction_date': '2026-08-01'},
        {'title': 'MYSTERY PAYMENT', 'counterpart': '', 'amount': -10.0, 'transaction_date': '2026-08-01'},
    ]
    monkeypatch.setattr(main, 'load_rules',
                        lambda: {'keywords': {'lidl': 'Groceries'}, 'merchant_map': {}, 'patterns': []})
    out = main.categorize_transactions(txs, cfg, api_key='')
    assert out[0]['category'] == 'Groceries'
    assert out[1]['category'] == 'CHECK ME'

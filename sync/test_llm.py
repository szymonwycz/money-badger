"""Checks for the LLM abstraction and the rules-only path.

No network: the OpenAI-compatible backend is exercised against a local HTTP
server, and the Anthropic one is never called here.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import llm
import main


def _clear_env():
    for k in ('LLM_PROVIDER', 'LLM_BASE_URL', 'LLM_MODEL', 'LLM_API_KEY', 'ANTHROPIC_API_KEY'):
        os.environ.pop(k, None)


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


def test_openai_backend_needs_a_base_url():
    _clear_env()
    os.environ['LLM_PROVIDER'] = 'openai'
    assert llm.configured() is False
    os.environ['LLM_BASE_URL'] = 'http://localhost:1/v1'
    assert llm.configured() is True
    _clear_env()


def test_extract_json_survives_fences_and_chatter():
    """Local models wrap answers in prose far more often than the hosted ones."""
    assert llm.extract_json('[{"a": 1}]') == [{'a': 1}]
    assert llm.extract_json('```json\n[{"a": 1}]\n```') == [{'a': 1}]
    assert llm.extract_json('Sure! Here you go:\n[{"a": 1}]\nHope that helps.') == [{'a': 1}]
    assert llm.extract_json('{"k": "v"}') == {'k': 'v'}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        _Handler.last_request = body
        reply = json.dumps({'choices': [{'message': {'content': '[{"idx": 0, "category": "Groceries"}]'}}]})
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(reply.encode())

    def log_message(self, *args):
        pass


def test_openai_backend_speaks_the_chat_completions_shape():
    server = HTTPServer(('127.0.0.1', 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _clear_env()
    os.environ['LLM_PROVIDER'] = 'openai'
    os.environ['LLM_BASE_URL'] = f'http://127.0.0.1:{server.server_port}/v1'
    os.environ['LLM_MODEL'] = 'qwen3:30b'
    try:
        out = llm.complete('categorize this', task='categorize', system='be terse')
        assert llm.extract_json(out) == [{'idx': 0, 'category': 'Groceries'}]
        sent = _Handler.last_request
        assert sent['model'] == 'qwen3:30b'
        assert sent['messages'][0] == {'role': 'system', 'content': 'be terse'}
        assert sent['messages'][1]['content'] == 'categorize this'
    finally:
        server.shutdown()
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

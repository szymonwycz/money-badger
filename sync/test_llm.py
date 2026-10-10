"""Checks for the LLM abstraction and the rules-only path.

No network: every OpenAI-compatible backend — the hosted ones included, they are
the same shape at a different URL — is exercised against a local HTTP server, and
the Anthropic one is never called here.
"""
import json
import io
import os
import sys
import urllib.error
import urllib.parse
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

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


class _FakeHTTPResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class _Stub:
    """A socket-free OpenAI-compatible /chat/completions and /models fake."""

    models = ()
    retired = ()
    list_broken = False
    requests = []

    @classmethod
    def urlopen(cls, request, timeout=None):
        body = json.loads(request.data) if request.data else None
        parsed = urllib.parse.urlparse(request.full_url)
        cls.requests.append({
            'path': parsed.path,
            'body': body,
            'auth': request.get_header('Authorization'),
            'timeout': timeout,
        })
        if request.get_method() == 'GET':
            if cls.list_broken:
                return cls._http_error(request.full_url, 500, {
                    'error': {'message': 'upstream on fire'}})
            return _FakeHTTPResponse({'data': [{'id': m} for m in cls.models]})

        if body['model'] in _Stub.retired:
            return cls._http_error(request.full_url, 404, {'error': {
                'code': 'model_not_found',
                'message': f"The model `{body['model']}` does not exist"}})
        if body['model'].startswith('gpt-5') and 'max_tokens' in body:
            return cls._http_error(request.full_url, 400, {'error': {'message':
                "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'."}})
        return _FakeHTTPResponse({'choices': [{'message': {
            'content': '[{"idx": 0, "category": "Groceries"}]'}}]})

    @staticmethod
    def _http_error(url, code, body):
        raw = json.dumps(body).encode()
        raise urllib.error.HTTPError(url, code, 'offline fake', {}, io.BytesIO(raw))


@contextmanager
def _stub(models=(), retired=(), list_broken=False):
    _Stub.models, _Stub.retired, _Stub.requests = models, retired, []
    _Stub.list_broken = list_broken
    _clear_env()
    with patch.object(llm.urllib.request, 'urlopen', _Stub.urlopen):
        try:
            yield 'http://offline.invalid/v1'
        finally:
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


class _FakeMessages:
    def __init__(self, reply, calls):
        self.reply = reply
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.reply


def _reply(stop_reason='end_turn', *blocks):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))


def _block(block_type, text=None):
    values = {'type': block_type}
    if text is not None:
        values['text'] = text
    return SimpleNamespace(**values)


def _install_fake_anthropic(monkeypatch, standard_reply=None, beta_reply=None):
    standard_calls = []
    beta_calls = []
    standard_reply = standard_reply or _reply('end_turn', _block('text', '{}'))
    beta_reply = beta_reply or standard_reply
    client = SimpleNamespace(
        messages=_FakeMessages(standard_reply, standard_calls),
        beta=SimpleNamespace(messages=_FakeMessages(beta_reply, beta_calls)),
    )

    class FakeNotFoundError(Exception):
        pass

    fake_module = SimpleNamespace(
        Anthropic=lambda **_kwargs: client,
        NotFoundError=FakeNotFoundError,
    )
    monkeypatch.setitem(sys.modules, 'anthropic', fake_module)
    return standard_calls, beta_calls


def _anthropic_env(monkeypatch, model=None):
    monkeypatch.setenv('LLM_PROVIDER', 'anthropic')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fake-key')
    monkeypatch.delenv('LLM_BASE_URL', raising=False)
    if model is None:
        monkeypatch.delenv('LLM_MODEL', raising=False)
    else:
        monkeypatch.setenv('LLM_MODEL', model)


def test_anthropic_joins_text_blocks_after_thinking(monkeypatch):
    _anthropic_env(monkeypatch)
    calls, _ = _install_fake_anthropic(
        monkeypatch,
        _reply('end_turn', _block('thinking'), _block('text', 'one'),
               _block('text', ' two')),
    )
    assert llm.complete('prompt', task='categorize') == 'one two'
    assert len(calls) == 1


@pytest.mark.parametrize(
    ('reply', 'error', 'message'),
    [
        (_reply('refusal'), llm.LLMRefusalError, 'refused'),
        (_reply('max_tokens', _block('text', 'partial')), llm.LLMResponseError, 'cut off'),
        (_reply('end_turn', _block('thinking')), llm.LLMResponseError, 'no text'),
    ],
)
def test_anthropic_rejects_refused_incomplete_or_textless_responses(
        monkeypatch, reply, error, message):
    _anthropic_env(monkeypatch)
    _install_fake_anthropic(monkeypatch, reply)
    with pytest.raises(error, match=message):
        llm.complete('prompt', task='categorize')


def test_categorize_uses_haiku_55_effort_and_large_budget(monkeypatch):
    _anthropic_env(monkeypatch)
    calls, beta_calls = _install_fake_anthropic(
        monkeypatch,
        _reply('end_turn', _block('text', '[{"idx": 0, "category": "Groceries"}]')),
    )
    monkeypatch.setattr(main, '_categories', (
        'EXPENSES:\n  Groceries\n', {'Groceries', 'CHECK ME'}))
    tx = {'title': 'SHOP', 'counterpart': '', 'amount': -10.0,
          'transaction_date': '2032-10-09'}

    main._categorize_batch([tx], api_key='fake-key')

    assert beta_calls == []
    sent = calls[0]
    assert sent['model'] == 'claude-haiku-5-5'
    assert sent['max_tokens'] == 16000
    assert sent['output_config'] == {'effort': 'medium'}
    for forbidden in ('temperature', 'top_p', 'top_k', 'thinking', 'fallbacks', 'betas'):
        assert forbidden not in sent


def test_learn_uses_sonnet_55_effort_and_server_fallback(monkeypatch):
    _anthropic_env(monkeypatch)
    calls, beta_calls = _install_fake_anthropic(
        monkeypatch,
        beta_reply=_reply('end_turn', _block('thinking'), _block('text', '{}')),
    )
    monkeypatch.setattr(main, '_categories', (
        'EXPENSES:\n  Groceries\n', {'Groceries', 'CHECK ME'}))

    assert main.generate_new_rules(
        [{'category': 'Groceries', 'description': 'SHOP', 'amount': -10.0}],
        api_key='fake-key') == {}

    assert calls == []
    sent = beta_calls[0]
    assert sent['model'] == 'claude-sonnet-5-5'
    assert sent['max_tokens'] == 16000
    assert sent['output_config'] == {'effort': 'medium'}
    assert sent['betas'] == ['server-side-fallback-2026-07-01']
    assert sent['fallbacks'] == 'default'
    for forbidden in ('temperature', 'top_p', 'top_k', 'thinking'):
        assert forbidden not in sent


def test_explicit_older_model_gets_no_55_only_parameters(monkeypatch):
    _anthropic_env(monkeypatch, model='claude-haiku-4-5-20251001')
    calls, beta_calls = _install_fake_anthropic(
        monkeypatch, _reply('end_turn', _block('text', 'answer')))
    assert llm.complete('prompt', task='learn', max_tokens=16000) == 'answer'
    assert beta_calls == []
    sent = calls[0]
    for forbidden in ('output_config', 'fallbacks', 'betas', 'thinking',
                      'temperature', 'top_p', 'top_k'):
        assert forbidden not in sent


def test_retired_haiku_substitution_stays_in_haiku_family():
    available = ['claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-opus-4-1']
    assert llm._closest('claude-haiku-4-5-20251001', available) == 'claude-haiku-5-5'


def test_categorize_refusal_becomes_check_me_without_retry(monkeypatch, capsys):
    _anthropic_env(monkeypatch)
    _install_fake_anthropic(monkeypatch, _reply('refusal'))
    monkeypatch.setattr(main, '_categories', (
        'EXPENSES:\n  Groceries\n', {'Groceries', 'CHECK ME'}))
    tx = {'title': 'MYSTERY', 'counterpart': '', 'amount': -10.0,
          'transaction_date': '2032-10-09'}
    out = main._categorize_batch([tx], api_key='fake-key')
    assert out[0]['category'] == 'CHECK ME'
    assert 'LLM refusal' in out[0]['note']
    assert 'refused' in capsys.readouterr().out


def test_learn_refusal_preserves_rules_and_correction(monkeypatch, tmp_path):
    _anthropic_env(monkeypatch)
    _install_fake_anthropic(monkeypatch, beta_reply=_reply('refusal'))
    corrections = tmp_path / 'Corrections'
    corrections.mkdir()
    correction = corrections / 'budget_corrections_test.csv'
    correction.write_text(
        'Date;Amount;Category;Description;Transaction Type\n'
        '2032-10-09;10.00;Groceries;SHOP;Expense\n', encoding='utf-8')
    rules = tmp_path / 'rules.json'
    original = '{"keywords": {"old": "Groceries"}}'
    rules.write_text(original, encoding='utf-8')
    monkeypatch.setattr(main, 'RULES_FILE', rules)
    monkeypatch.setattr(main, 'load_config', lambda: {'corrections_dir': str(corrections)})
    monkeypatch.setattr(main, 'load_env', lambda: {'ANTHROPIC_API_KEY': 'fake-key'})
    monkeypatch.setattr(main, '_categories', (
        'EXPENSES:\n  Groceries\n', {'Groceries', 'CHECK ME'}))

    with pytest.raises(llm.LLMRefusalError):
        main.cmd_learn(SimpleNamespace(dry_run=False))

    assert rules.read_text(encoding='utf-8') == original
    assert correction.exists()
    assert not (corrections / 'processed').exists()


def test_learn_fake_llm_does_not_save_holder_only_rule(monkeypatch, tmp_path):
    _anthropic_env(monkeypatch)
    learned = {
        'keywords': {'Alex Example': 'Groceries'},
        'merchant_map': {},
        'patterns': [
            {'pattern': ' ALEX   EXAMPLE ', 'category': 'Groceries', 'priority': 1},
            {'pattern': 'therapy alex example', 'category': 'Groceries', 'priority': 1},
        ],
    }
    _install_fake_anthropic(
        monkeypatch,
        beta_reply=_reply('end_turn', _block('text', json.dumps(learned))),
    )
    corrections = tmp_path / 'Corrections'
    corrections.mkdir()
    correction = corrections / 'budget_corrections_test.csv'
    correction.write_text(
        'Date;Amount;Category;Description;Transaction Type\n'
        '2032-10-09;10.00;Groceries;therapy payment;Expense\n', encoding='utf-8')
    rules = tmp_path / 'rules.json'
    rules.write_text(
        '{"keywords": {}, "merchant_map": {}, "patterns": []}', encoding='utf-8')
    monkeypatch.setattr(main, 'RULES_FILE', rules)
    monkeypatch.setattr(main, 'load_config', lambda: {
        'corrections_dir': str(corrections),
        'account_holders': [' alex example '],
    })
    monkeypatch.setattr(main, 'load_env', lambda: {'ANTHROPIC_API_KEY': 'fake-key'})
    monkeypatch.setattr(main, '_categories', (
        'EXPENSES:\n  Groceries\n', {'Groceries', 'CHECK ME'}))

    main.cmd_learn(SimpleNamespace(dry_run=False))

    saved = json.loads(rules.read_text(encoding='utf-8'))
    assert saved['keywords'] == {}
    assert [p['pattern'] for p in saved['patterns']] == ['therapy alex example']
    assert not correction.exists()
    assert (corrections / 'processed' / correction.name).exists()

"""One call, two backends: Anthropic's API or any OpenAI-compatible endpoint.

The second one covers the hosted providers listed below as well as Ollama,
LM Studio and vLLM, which is what makes a fully local install possible — no key,
no data leaving the house.

Configured through the environment (see .env.example):

    LLM_PROVIDER=anthropic          ANTHROPIC_API_KEY=sk-ant-...
    LLM_PROVIDER=openai             OPENAI_API_KEY=sk-...
    LLM_PROVIDER=deepseek           DEEPSEEK_API_KEY=...
    LLM_PROVIDER=moonshot           MOONSHOT_API_KEY=...        (Kimi)
    LLM_PROVIDER=gemini             GEMINI_API_KEY=...
    LLM_PROVIDER=xai                XAI_API_KEY=...             (Grok)
    LLM_PROVIDER=openai             LLM_BASE_URL=http://box:11434/v1
                                    LLM_MODEL=qwen3:30b
                                    LLM_API_KEY=   (usually unused locally)

LLM_BASE_URL overrides the provider's URL, because that is all a local server is:
an OpenAI-compatible endpoint somewhere else.

Model ids are not pinned. install.sh asks the provider what it has — list_models(),
also reachable as `python sync/llm.py --models` — and writes the answer to
LLM_MODEL. When that id is later retired, complete() asks again, picks the nearest
model of the same family and says so in the log rather than dying mid-sync.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import env_file

env_file.load()

# provider -> (base URL, environment variable holding the key). Everything but
# Anthropic speaks the OpenAI chat-completions and /models shapes, so adding one
# is a line here, not a backend.
PROVIDERS = {
    'anthropic': ('https://api.anthropic.com/v1', 'ANTHROPIC_API_KEY'),
    'openai': ('https://api.openai.com/v1', 'OPENAI_API_KEY'),
    'deepseek': ('https://api.deepseek.com/v1', 'DEEPSEEK_API_KEY'),
    'moonshot': ('https://api.moonshot.ai/v1', 'MOONSHOT_API_KEY'),
    'gemini': ('https://generativelanguage.googleapis.com/v1beta/openai', 'GEMINI_API_KEY'),
    'xai': ('https://api.x.ai/v1', 'XAI_API_KEY'),
}

ANTHROPIC_VERSION = '2023-06-01'

DEFAULT_MODELS = {
    # Only used when LLM_MODEL is unset, which after install means an Anthropic
    # install from before the model picker existed. Categorization runs on every
    # unmatched transaction, so it wants the cheap model; rule learning runs once
    # a day over a handful of examples and wants the better one. Both are
    # eventually retired, and _substitute() is what carries those installs over.
    'categorize': 'claude-haiku-4-5-20251001',
    'learn': 'claude-sonnet-4-6',
}

# Requested id -> what the provider actually had, for the rest of this process.
_substitutes = {}


class ModelNotFound(RuntimeError):
    """The configured model id is gone. The provider's list is the way back."""


class _ParamRejected(RuntimeError):
    """The provider refused a request parameter — not the model itself."""


def provider() -> str:
    return os.environ.get('LLM_PROVIDER', 'anthropic').strip().lower()


def configured() -> bool:
    """True when there is something to call. Callers fall back to rules-only."""
    if os.environ.get('LLM_BASE_URL'):
        return True  # a local server, which usually wants no key at all
    return bool(_key())


def _key(api_key: str = None) -> str:
    if api_key:
        return api_key
    override = os.environ.get('LLM_API_KEY')
    if override:
        return override
    _, key_env = PROVIDERS.get(provider(), (None, None))
    return os.environ.get(key_env, '') if key_env else ''


def _base() -> str:
    base = os.environ.get('LLM_BASE_URL') or PROVIDERS.get(provider(), ('',))[0]
    if not base:
        raise RuntimeError(
            f'Unknown LLM_PROVIDER={provider()!r}. Use one of: '
            f'{", ".join(sorted(PROVIDERS))} — or set LLM_BASE_URL to your own '
            f'OpenAI-compatible server.')
    return base.rstrip('/')


def _model(task: str) -> str:
    model = os.environ.get('LLM_MODEL')
    if model:
        return model
    if provider() == 'anthropic':
        return DEFAULT_MODELS[task]
    raise RuntimeError(
        f'LLM_MODEL is not set and {provider()} has no default. Run '
        f'`python sync/llm.py --models` to see what it offers, then put one in .env.')


def _request(url: str, headers: dict, payload=None, timeout: int = 300):
    """One JSON round trip. Turns the interesting failures into typed errors."""
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method='POST' if data else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace').strip()[:500]
        if _model_is_gone(detail):
            raise ModelNotFound(detail) from None
        if 'max_completion_tokens' in detail:
            raise _ParamRejected(detail) from None
        raise RuntimeError(f'{provider()} returned HTTP {e.code}: {detail}') from None
    except urllib.error.URLError as e:
        raise RuntimeError(f'{provider()} at {url} is unreachable: {e.reason}') from None


def _model_is_gone(detail: str) -> bool:
    """Every provider words a retired model differently; none of them are subtle."""
    text = detail.lower()
    return 'model' in text and any(
        phrase in text for phrase in
        ('not_found', 'not found', 'does not exist', "doesn't exist",
         'unknown model', 'invalid model', 'decommissioned', 'deprecated'))


def list_models(api_key: str = None) -> list:
    """Model ids the configured provider offers. Used by install.sh's picker.

    Includes whatever else the provider sells — embeddings, speech — because
    filtering by name guesses wrong the moment a family is renamed.
    """
    base = _base()
    key = _key(api_key)
    if provider() == 'anthropic' and not os.environ.get('LLM_BASE_URL'):
        headers = {'x-api-key': key, 'anthropic-version': ANTHROPIC_VERSION}
        url = f'{base}/models?limit=1000'
    else:
        headers = {'Authorization': f'Bearer {key}'} if key else {}
        url = f'{base}/models'
    body = _request(url, headers, timeout=30)
    # Gemini's compatibility layer prefixes ids with models/; chat/completions
    # takes them either way, and bare ids keep the family matching honest.
    return sorted(str(m['id']).removeprefix('models/')
                  for m in body.get('data', []) if m.get('id'))


def _closest(model: str, available: list):
    """The nearest id of the same family, or None when the family is gone too."""
    family = re.split(r'[-._:/]', model)[0].lower()
    same = [m for m in available if re.split(r'[-._:/]', m)[0].lower() == family]
    if not same:
        return None
    return max(same, key=lambda m: (len(os.path.commonprefix([model, m])), m))


def _substitute(model: str, why: Exception, api_key: str = None) -> str:
    """Pick a live model for a retired id — loudly, never behind your back."""
    try:
        available = list_models(api_key)
    except Exception as e:
        raise RuntimeError(
            f'Model {model!r} is gone ({why}) and {provider()} would not list its '
            f'models either ({e}). Set LLM_MODEL in .env to an id that exists.') from None
    pick = _closest(model, available)
    if not pick:
        raise RuntimeError(
            f'Model {model!r} is gone ({why}) and {provider()} offers nothing in the '
            f'same family. Set LLM_MODEL in .env to one of: {", ".join(available[:15]) or "(nothing)"}')
    print(f'  [llm] model {model} is gone at {provider()} — using {pick} instead. '
          f'Set LLM_MODEL={pick} in .env to make it permanent.')
    _substitutes[model] = pick
    return pick


def complete(prompt: str, task: str = 'categorize', system: str = None,
             max_tokens: int = 4000, api_key: str = None) -> str:
    """Send one prompt, return the text of the reply."""
    model = _model(task)
    model = _substitutes.get(model, model)  # already repaired earlier in this run
    try:
        return _send(model, prompt, system, max_tokens, api_key)
    except ModelNotFound as e:
        return _send(_substitute(model, e, api_key), prompt, system, max_tokens, api_key)


def _send(model, prompt, system, max_tokens, api_key):
    if provider() == 'anthropic' and not os.environ.get('LLM_BASE_URL'):
        return _complete_anthropic(model, prompt, system, max_tokens, api_key)
    return _complete_openai(model, prompt, system, max_tokens)


def _complete_anthropic(model, prompt, system, max_tokens, api_key):
    # Imported here, not at module level: the BASIC and bank-only installs never
    # call this and shouldn't need the SDK installed at all.
    import anthropic

    client = anthropic.Anthropic(api_key=api_key or os.environ.get('ANTHROPIC_API_KEY'))
    kwargs = {'model': model, 'max_tokens': max_tokens,
              'messages': [{'role': 'user', 'content': prompt}]}
    if system:
        kwargs['system'] = system
    try:
        msg = client.messages.create(**kwargs)
    except anthropic.NotFoundError as e:
        raise ModelNotFound(str(e)) from None
    return msg.content[0].text


def _complete_openai(model, prompt, system, max_tokens):
    messages = ([{'role': 'system', 'content': system}] if system else [])
    messages.append({'role': 'user', 'content': prompt})
    payload = {'model': model, 'messages': messages, 'max_tokens': max_tokens}

    headers = {'Content-Type': 'application/json'}
    key = _key()
    if key:
        headers['Authorization'] = f'Bearer {key}'

    # Local models on modest hardware are slow; a minute is not unusual.
    url = f'{_base()}/chat/completions'
    try:
        body = _request(url, headers, payload)
    except _ParamRejected:
        # OpenAI's newer models take max_completion_tokens and reject max_tokens.
        payload['max_completion_tokens'] = payload.pop('max_tokens')
        body = _request(url, headers, payload)
    return body['choices'][0]['message']['content']


def extract_json(raw: str):
    """Parse a JSON reply, tolerating the ```json fences models like to add.

    Local models are chattier than the hosted ones and often wrap the answer in
    prose, so fall back to the outermost bracketed span.
    """
    text = raw.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        starts = [i for i in (text.find('['), text.find('{')) if i != -1]
        if not starts:
            raise
        start = min(starts)
        end = max(text.rfind(']'), text.rfind('}'))
        return json.loads(text[start:end + 1])


if __name__ == '__main__':
    # install.sh calls this with the provider and key in the environment, to show
    # a picker instead of asking anyone to memorize model ids.
    if '--models' not in sys.argv:
        sys.exit('usage: python sync/llm.py --models')
    try:
        print('\n'.join(list_models()))
    except Exception as exc:
        sys.exit(str(exc))

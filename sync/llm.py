"""One call, two backends: Anthropic's API or any OpenAI-compatible endpoint.

The second covers Ollama, LM Studio, vLLM and OpenAI itself, which is what makes
a fully local install possible — no key, no data leaving the house.

Configured through the environment (see .env.example):

    LLM_PROVIDER=anthropic          ANTHROPIC_API_KEY=sk-ant-...
    LLM_PROVIDER=openai             LLM_BASE_URL=http://box:11434/v1
                                    LLM_MODEL=qwen3:30b
                                    LLM_API_KEY=   (usually unused locally)

Both models default to Anthropic's, which is what this pipeline was tuned on.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import env_file

env_file.load()

DEFAULT_MODELS = {
    # Categorization runs on every unmatched transaction, so it wants the cheap
    # model; rule learning runs once a day over a handful of examples and wants
    # the better one.
    'categorize': 'claude-haiku-4-5-20251001',
    'learn': 'claude-sonnet-4-6',
}


def provider() -> str:
    return os.environ.get('LLM_PROVIDER', 'anthropic').strip().lower()


def configured() -> bool:
    """True when there is something to call. Callers fall back to rules-only."""
    if provider() == 'openai':
        return bool(os.environ.get('LLM_BASE_URL'))
    return bool(os.environ.get('ANTHROPIC_API_KEY'))


def _model(task: str) -> str:
    return os.environ.get('LLM_MODEL') or DEFAULT_MODELS[task]


def complete(prompt: str, task: str = 'categorize', system: str = None,
             max_tokens: int = 4000, api_key: str = None) -> str:
    """Send one prompt, return the text of the reply."""
    if provider() == 'openai':
        return _complete_openai(prompt, task, system, max_tokens)
    return _complete_anthropic(prompt, task, system, max_tokens, api_key)


def _complete_anthropic(prompt, task, system, max_tokens, api_key):
    # Imported here, not at module level: the BASIC and bank-only installs never
    # call this and shouldn't need the SDK installed at all.
    import anthropic

    client = anthropic.Anthropic(api_key=api_key or os.environ.get('ANTHROPIC_API_KEY'))
    kwargs = {'model': _model(task), 'max_tokens': max_tokens,
              'messages': [{'role': 'user', 'content': prompt}]}
    if system:
        kwargs['system'] = system
    msg = client.messages.create(**kwargs)
    return msg.content[0].text


def _complete_openai(prompt, task, system, max_tokens):
    base = os.environ.get('LLM_BASE_URL', '').rstrip('/')
    if not base:
        raise RuntimeError('LLM_PROVIDER=openai but LLM_BASE_URL is not set')

    messages = ([{'role': 'system', 'content': system}] if system else [])
    messages.append({'role': 'user', 'content': prompt})
    payload = {'model': _model(task), 'messages': messages, 'max_tokens': max_tokens}

    headers = {'Content-Type': 'application/json'}
    key = os.environ.get('LLM_API_KEY')
    if key:
        headers['Authorization'] = f'Bearer {key}'

    req = urllib.request.Request(f'{base}/chat/completions',
                                 data=json.dumps(payload).encode('utf-8'),
                                 headers=headers, method='POST')
    # Local models on modest hardware are slow; a minute is not unusual.
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read())
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

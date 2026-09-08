"""The one place that talks to the budget app's HTTP API.

Every part of the pipeline reaches the app the same way — over HTTP, never by
opening budget.db directly — so authentication, timeouts and the base URL are
settled here instead of in five call sites.

BUDGET_URL points at the app. The default assumes the pipeline runs on the same
host as the web service; set it in .env when it doesn't.
"""
import json
import os
import urllib.error
import urllib.request

import env_file

# Loaded here rather than relying on the importer having done it: these two
# values are read at import time, so an import ordering that put this module
# first would silently leave the pipeline unauthenticated.
env_file.load()

BUDGET_URL = os.environ.get('BUDGET_URL', 'http://127.0.0.1:5055').rstrip('/')

# Once the app has a password the pipeline can't log in like a browser, so it
# presents this token instead. Set both in .env; empty means the app has no auth.
API_TOKEN = os.environ.get('MB_API_TOKEN', '')

HTTPError = urllib.error.HTTPError
URLError = urllib.error.URLError


def request(path, method='GET', payload=None, timeout=20, with_headers=False,
            raw=False):
    """Call the API. Returns the decoded body, or (body, headers) when asked.

    Errors are not swallowed: callers decide whether a failure is fatal, and
    they need to tell a 4xx apart from the server being down.
    """
    url = path if path.startswith('http') else f'{BUDGET_URL}{path}'
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if API_TOKEN:
        headers['X-MB-Token'] = API_TOKEN
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        resp_headers = dict(resp.headers)
    # /api/corrections answers with CSV, not JSON. Decoding everything as JSON
    # is how the corrections → learn loop dies silently: the pull raises,
    # master_pi finds no CSV to feed learn, and the rules stop growing while
    # the corrections pile up unsynced.
    parsed = body.decode('utf-8') if raw else (json.loads(body) if body else None)
    return (parsed, resp_headers) if with_headers else parsed


def get(path, timeout=10, with_headers=False):
    return request(path, timeout=timeout, with_headers=with_headers)


def post(path, payload, timeout=30):
    return request(path, method='POST', payload=payload, timeout=timeout)


def put(path, payload, timeout=30):
    return request(path, method='PUT', payload=payload, timeout=timeout)

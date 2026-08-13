"""Read .env into the environment.

systemd does this for the service through EnvironmentFile, but not when someone
runs gunicorn or a sync command by hand — and then the app comes up with no
password and the pipeline with no API token, which looks like a config bug.

Real environment variables always win, so a value exported in the shell (or by
systemd) overrides the file rather than the other way round.
"""
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')


def load(path=None):
    """Load key=value lines. Missing file is fine — everything has a default."""
    path = path or _PATH
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

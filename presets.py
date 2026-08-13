"""Starter data for a fresh database.

Migrations build the schema; presets fill it with a category tree, income
categories and accounts so a new install isn't a blank page. The setup screen
applies one on first run, and tests use one as a fixture.

A preset is a suggestion, not a contract — everything it creates is editable
in the UI afterwards.
"""
import json
import os

PRESET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'presets')

# The CHECK ME income category is the review inbox for anything the importer
# can't place. The pipeline writes that literal name, so a preset without it
# would send unrecognized income into whatever category sorts first.
REQUIRED_INCOME_CATEGORY = 'CHECK ME'


def list_presets():
    """Available presets as [{id, name, description}], for the setup screen."""
    out = []
    for fn in sorted(os.listdir(PRESET_DIR)):
        if not fn.endswith('.json'):
            continue
        p = load_preset(fn[:-5])
        out.append({'id': fn[:-5], 'name': p['name'], 'description': p.get('description', '')})
    return out


def load_preset(preset_id):
    """Read a preset by id. Rejects anything that isn't a plain filename."""
    if not preset_id or '/' in preset_id or '.' in preset_id:
        raise ValueError(f'invalid preset id: {preset_id!r}')
    with open(os.path.join(PRESET_DIR, preset_id + '.json'), encoding='utf-8') as f:
        preset = json.load(f)
    if REQUIRED_INCOME_CATEGORY not in preset.get('income_categories', []):
        raise ValueError(f'preset {preset_id!r} is missing the {REQUIRED_INCOME_CATEGORY} income category')
    return preset


def apply_preset(db, preset, categories=None, income_categories=None, accounts=None):
    """Seed an open database connection from a preset.

    Each of the three lists can be overridden with a user-edited version from
    the setup screen; pass None to take the preset's own. Tables that already
    hold rows are left alone, so this is safe to call twice.
    """
    cats = preset['categories'] if categories is None else categories
    incomes = preset['income_categories'] if income_categories is None else income_categories
    accts = preset.get('accounts', []) if accounts is None else accounts

    if not db.execute('SELECT 1 FROM categories LIMIT 1').fetchone():
        for order, group in enumerate(cats, start=1):
            cur = db.execute(
                'INSERT INTO categories (name, parent_id, sort_order) VALUES (?, NULL, ?)',
                (group['name'], order))
            for i, child in enumerate(group.get('children', [])):
                db.execute(
                    'INSERT INTO categories (name, parent_id, sort_order) VALUES (?, ?, ?)',
                    (child, cur.lastrowid, i))

    if not db.execute('SELECT 1 FROM income_categories LIMIT 1').fetchone():
        if REQUIRED_INCOME_CATEGORY not in incomes:
            incomes = list(incomes) + [REQUIRED_INCOME_CATEGORY]
        for i, name in enumerate(incomes):
            db.execute('INSERT INTO income_categories (name, sort_order) VALUES (?, ?)', (name, i))

    if not db.execute('SELECT 1 FROM accounts LIMIT 1').fetchone():
        for entry in accts:
            name, atype = (entry['name'], entry.get('type', 'bank')) if isinstance(entry, dict) else entry
            db.execute('INSERT INTO accounts (name, type) VALUES (?, ?)', (name, atype))

    db.commit()

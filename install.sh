#!/usr/bin/env bash
# Money Badger installer. Sets up the environment only — accounts, categories
# and the rest are configured in the browser afterwards, at /setup.
#
#   ./install.sh
#
# Safe to re-run: it never overwrites an existing .env or database.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_MIN="3.9"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# Read from the terminal when there is one, so `curl ... | bash` still asks
# questions instead of eating the script as answers. Falls back to stdin, which
# is what makes the answers pipeable for an unattended install.
# -r only checks permissions; /dev/tty exists but can't be opened when there is
# no controlling terminal, so try it for real.
if { : </dev/tty; } 2>/dev/null; then INPUT=/dev/tty; else INPUT=/dev/stdin; fi

ask() {  # ask <prompt> <default>
    local reply=""
    read -r -p "$1 [$2]: " reply <"$INPUT" || true
    printf '%s' "${reply:-$2}"
}

ask_secret() {
    local reply=""
    if [ "$INPUT" = /dev/tty ]; then
        read -r -s -p "$1: " reply <"$INPUT" || true
        printf '\n' >&2
    else
        read -r -p "$1: " reply <"$INPUT" || true
    fi
    printf '%s' "$reply"
}

# ── 1. what's already here ────────────────────────────────────────────────────

say "Checking prerequisites"

command -v python3 >/dev/null || die "python3 not found. Install it (apt install python3 python3-venv) and re-run."

PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c "import sys; sys.exit(0 if sys.version_info >= tuple(int(x) for x in '$PYTHON_MIN'.split('.')) else 1)" \
    || die "Python $PYTHON_MIN or newer required, found $PYVER."
echo "  python3 $PYVER"

python3 -c 'import venv' 2>/dev/null || die "The venv module is missing. Install python3-venv and re-run."
echo "  venv module present"

if command -v sqlite3 >/dev/null; then
    echo "  sqlite3 $(sqlite3 --version | cut -d' ' -f1)"
else
    warn "  sqlite3 CLI not found — the app itself is fine without it, but the"
    warn "  backup script (deploy/backup-db.sh) needs it."
fi

command -v git >/dev/null && echo "  git $(git --version | cut -d' ' -f3)" \
    || warn "  git not found — only needed for the optional offsite backup mirror."

# ── 2. questions ──────────────────────────────────────────────────────────────

say "Configuration"

PORT=$(ask "Port to serve on" "5055")
[[ "$PORT" =~ ^[0-9]+$ ]] || die "Port must be a number."
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "Port must be between 1 and 65535."

# Chrome and Firefox refuse to connect to these regardless of what is listening,
# with an error that doesn't mention the port — a confusing way to lose an evening.
case " 1719 1720 1723 2049 3659 4045 5060 5061 6000 6566 6665 6666 6667 6668 6669 6697 10080 " in
    *" $PORT "*) die "Browsers block port $PORT as unsafe. Pick another one." ;;
esac

echo
echo "  1) basic       — enter transactions yourself"
echo "  2) bank sync   — also pull transactions from your bank (Enable Banking)"
echo "  3) bank + LLM  — and categorize them automatically"
VARIANT=$(ask "Which variant" "1")
[[ "$VARIANT" =~ ^[123]$ ]] || die "Pick 1, 2 or 3."

echo
echo "  A password protects every page and the whole transaction history."
echo "  Leave it empty only if nothing but your own machine can reach this port."
PASSWORD=$(ask_secret "  Password (empty = no login)")

ADMIN_USER="admin"
if [ -n "$PASSWORD" ]; then
    echo
    echo "  This becomes your account. You can add accounts for other people"
    echo "  later from the ACCOUNT tab — everyone shares the same budget."
    ADMIN_USER=$(ask "  Your username" "admin")
    [[ "$ADMIN_USER" =~ ^[A-Za-z0-9._-]{2,32}$ ]] \
        || die "Username must be 2-32 characters: letters, digits, dot, dash or underscore."
fi

SYNC_TIME="10:30"; SYNC_RUNS=4
if [ "$VARIANT" != "1" ]; then
    echo
    echo "  When should the daily bank fetch run? Pick a time after your bank has"
    echo "  posted the previous day — late morning is usually safe."
    SYNC_TIME=$(ask "  Time (HH:MM)" "10:30")
    [[ "$SYNC_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || die "Use HH:MM, e.g. 10:30."

    echo
    echo "  If a fetch fails — bank down, no network — a watchdog retries later the"
    echo "  same day. Retries are skipped once the day has succeeded, but a run that"
    echo "  keeps failing spends one bank API call each time, and most banks allow"
    echo "  only a few per day."
    SYNC_RUNS=$(ask "  Attempts per day in total (1-5)" "4")
    [[ "$SYNC_RUNS" =~ ^[1-5]$ ]] || die "Pick a number from 1 to 5."
fi

LLM_PROVIDER=""; API_KEY=""; KEY_VAR=""; LLM_BASE_URL=""; LLM_MODEL=""
if [ "$VARIANT" = "3" ]; then
    echo
    echo "  Hosted providers cost a few cents a month and need an API key."
    echo "  A local model costs nothing and never sends your transactions anywhere."
    echo
    echo "  1) Anthropic (Claude)   2) OpenAI (GPT)      3) DeepSeek"
    echo "  4) Kimi (Moonshot)      5) Google Gemini     6) xAI (Grok)"
    echo "  7) Local model — Ollama, LM Studio, anything OpenAI-compatible"
    LLM_CHOICE=$(ask "  Which" "1")

    case "$LLM_CHOICE" in
        1) LLM_PROVIDER="anthropic"; KEY_VAR="ANTHROPIC_API_KEY" ;;
        2) LLM_PROVIDER="openai";    KEY_VAR="OPENAI_API_KEY" ;;
        3) LLM_PROVIDER="deepseek";  KEY_VAR="DEEPSEEK_API_KEY" ;;
        4) LLM_PROVIDER="moonshot";  KEY_VAR="MOONSHOT_API_KEY" ;;
        5) LLM_PROVIDER="gemini";    KEY_VAR="GEMINI_API_KEY" ;;
        6) LLM_PROVIDER="xai";       KEY_VAR="XAI_API_KEY" ;;
        7) LLM_PROVIDER="openai"
           LLM_BASE_URL=$(ask "  Base URL" "http://127.0.0.1:11434/v1")
           LLM_MODEL=$(ask "  Model" "qwen3:30b") ;;
        *) die "Pick a number from 1 to 7." ;;
    esac

    # Hosted providers: pick the model from what the account can actually see,
    # so nothing here goes stale when the provider renames or retires one.
    # sync/llm.py is stdlib-only on purpose, so this runs before the venv exists.
    if [ -n "$KEY_VAR" ]; then
        API_KEY=$(ask_secret "  $KEY_VAR")
        if [ -n "$API_KEY" ]; then
            echo "  Asking $LLM_PROVIDER which models this key can use..."
            if MODELS=$(env LLM_PROVIDER="$LLM_PROVIDER" "$KEY_VAR=$API_KEY" \
                        python3 "$APP_DIR/sync/llm.py" --models 2>&1); then
                echo "$MODELS" | nl -w4 -s') '
                echo
                echo "  That is everything the provider sells, embeddings and all."
                echo "  Cheap and fast is usually right — this runs on every unmatched"
                echo "  transaction. Enter empty to decide later in .env."
                PICK=$(ask "  Which model (number, or paste an id)" "")
                if [[ "$PICK" =~ ^[0-9]+$ ]]; then
                    LLM_MODEL=$(echo "$MODELS" | sed -n "${PICK}p")
                    [ -n "$LLM_MODEL" ] || die "There is no model $PICK in that list."
                    echo "  Using $LLM_MODEL"
                else
                    LLM_MODEL="$PICK"
                fi
            else
                warn "  Could not list models: $MODELS"
                LLM_MODEL=$(ask "  Model id (empty = set LLM_MODEL in .env later)" "")
            fi
        else
            warn "  No key given — categorization stays off until you set it in .env."
        fi
    fi
fi

TELEGRAM_BOT_TOKEN=""; TELEGRAM_CHAT_ID=""
if [ "$VARIANT" != "1" ]; then
    echo
    echo "  Telegram can tell you what each sync did, and send the nightly audit"
    echo "  in the morning — otherwise a bank that quietly stops returning data is"
    echo "  something you notice weeks later. Skip it and everything still works."
    echo
    echo "  To set one up:"
    echo "    1. message @BotFather on Telegram, send /newbot, follow the prompts"
    echo "    2. it replies with a token like 123456789:AAH..."
    echo "    3. message @userinfobot to get your numeric chat id"
    echo "    4. send your new bot any message once, or it cannot write to you"
    echo
    echo "  Use a bot dedicated to this app. The token is the entire credential,"
    echo "  so anything else holding it can post as this app."
    if [ "$(ask "  Set up Telegram notifications? (y/n)" "n")" = "y" ]; then
        TELEGRAM_BOT_TOKEN=$(ask_secret "  Bot token")
        TELEGRAM_CHAT_ID=$(ask "  Chat id" "")
        [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ] \
            || warn "  Both are needed — notifications stay off until you fill them in .env."
    fi
fi

MAIL_ADDRESS=""; MAIL_PASSWORD=""; MAIL_IMAP_HOST=""; MAIL_IMAP_PORT=""
SHOPS=""; LOCAL_CURRENCY=""; FX_PAIRS=""
if [ "$VARIANT" != "1" ]; then
    echo
    echo "  Money Badger can read order confirmations from a mailbox and put the"
    echo "  shop and the items into the matching bank transaction, so a card charge"
    echo "  stops being an anonymous number."
    if [ "$(ask "  Match orders from email? (y/n)" "n")" = "y" ]; then
        echo
        echo "  Use an app password if your provider offers one — this is stored"
        echo "  in plain text in .env, like every other credential here."
        MAIL_ADDRESS=$(ask "  Mailbox address" "")
        MAIL_PASSWORD=$(ask_secret "  Mailbox password")
        echo
        echo "  Implicit TLS only (the usual port 993). A server that only speaks"
        echo "  STARTTLS will not work."
        MAIL_IMAP_HOST=$(ask "  IMAP server" "imap.gmail.com")
        MAIL_IMAP_PORT=$(ask "  IMAP port" "993")

        if [ -z "$MAIL_ADDRESS" ] || [ -z "$MAIL_PASSWORD" ]; then
            warn "  Address and password are both needed — order matching stays off."
            MAIL_ADDRESS=""; MAIL_PASSWORD=""
        else
            echo
            echo "  Each shop needs its own folder in that mailbox, named exactly"
            echo "  after the shop. On Gmail that is a filter applying a label."
            SHOPS=$(ask "  Which shops (comma-separated)" "Allegro")
            case "$SHOPS" in
                *,*|*[Aa]mazon*|*[Ee]bay*|*[Tt]emu*|*[Aa]li*)
                    echo
                    echo "  Foreign orders need a rough exchange rate — your bank converts"
                    echo "  at its own rate, so this only has to be close enough to"
                    echo "  recognise the charge. Leave one empty to review those by hand."
                    LOCAL_CURRENCY=$(ask "  Your account currency (ISO code)" "PLN")
                    for CUR in EUR USD; do
                        RATE=$(ask "  Approximate $CUR -> $LOCAL_CURRENCY rate" "")
                        [ -n "$RATE" ] && FX_PAIRS="$FX_PAIRS$CUR=$RATE "
                    done ;;
            esac
        fi
    fi
fi

MARKET=""
if [ "$VARIANT" = "3" ]; then
    PACKS=$(ls "$APP_DIR"/sync/market/*.json 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\.json$//' | tr '\n' ' ')
    if [ -n "$PACKS" ]; then
        echo
        echo "  A market pack tells the categorizer what your bank's wording looks"
        echo "  like locally. Without one it still works, just with fewer hints."
        echo "  Available: $PACKS"
        MARKET=$(ask "  Market pack (empty = none)" "")
    fi
fi

B2B_VAT=""; B2B_TAX=""
echo
echo "  If you invoice as a business, the calculator tab can set aside VAT and"
echo "  income tax from what you bill. Rates are editable in the app afterwards."
if [ "$(ask "  Do you work B2B / self-employed? (y/n)" "n")" = "y" ]; then
    B2B_VAT=$(ask "  VAT rate in percent (empty = skip)" "")
    B2B_TAX=$(ask "  Income tax rate in percent (empty = skip)" "")
    for RATE in "$B2B_VAT" "$B2B_TAX"; do
        [ -z "$RATE" ] || [[ "$RATE" =~ ^[0-9]+([.][0-9]+)?$ ]] \
            || die "Rates are plain numbers, e.g. 23 or 8.5 — got '$RATE'."
    done
fi

# ── 3. virtualenv ─────────────────────────────────────────────────────────────

say "Installing dependencies"

[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
echo "  core: flask, gunicorn"

if [ "$VARIANT" != "1" ]; then
    if [ "$LLM_PROVIDER" != "anthropic" ]; then
        # The Anthropic SDK is only needed for the hosted provider.
        "$APP_DIR/venv/bin/pip" install --quiet requests
        echo "  sync: requests"
    else
        "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements-sync.txt"
        echo "  sync: requests, anthropic"
    fi
fi

# ── 4. .env ───────────────────────────────────────────────────────────────────

say "Writing configuration"

if [ -f "$APP_DIR/.env" ]; then
    warn "  .env already exists — leaving it alone."
else
    SECRET_KEY=$("$APP_DIR/venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))')
    API_TOKEN=$("$APP_DIR/venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))')
    PASSWORD_HASH=""
    if [ -n "$PASSWORD" ]; then
        PASSWORD_HASH=$(MB_PW="$PASSWORD" "$APP_DIR/venv/bin/python" -c \
            'import os; from werkzeug.security import generate_password_hash as h; print(h(os.environ["MB_PW"]))')
    fi

    umask 077
    cat > "$APP_DIR/.env" <<EOF
# Written by install.sh. See .env.example for everything else you can set.
MB_PASSWORD_HASH=$PASSWORD_HASH
MB_ADMIN_USER=$ADMIN_USER
SECRET_KEY=$SECRET_KEY
MB_API_TOKEN=$API_TOKEN
BUDGET_URL=http://127.0.0.1:$PORT

LLM_PROVIDER=$LLM_PROVIDER
${KEY_VAR:-ANTHROPIC_API_KEY}=$API_KEY
LLM_BASE_URL=$LLM_BASE_URL
LLM_MODEL=$LLM_MODEL

TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID

MAIL_ADDRESS=$MAIL_ADDRESS
MAIL_PASSWORD=$MAIL_PASSWORD
MAIL_IMAP_HOST=$MAIL_IMAP_HOST
MAIL_IMAP_PORT=$MAIL_IMAP_PORT
EOF
    umask 022
    echo "  .env written (mode 600)"
    if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        if "$APP_DIR/venv/bin/python" "$APP_DIR/notify_telegram.py" \
               "Money Badger installed — notifications are working." >/dev/null 2>&1; then
            echo "  Telegram: test message sent"
        else
            warn "  Telegram: test message failed. Check the token and chat id in .env,"
            warn "  and that you have sent your bot a message at least once."
        fi
    fi
    [ -z "$PASSWORD_HASH" ] && warn "  No password set — the app is open to anyone who can reach the port."
fi

if [ "$VARIANT" != "1" ] && [ ! -f "$APP_DIR/sync/config.json" ]; then
    cp "$APP_DIR/sync/config.example.json" "$APP_DIR/sync/config.json"
    echo "  sync/config.json created from the example — edit it before the first fetch"

    # Patched as JSON, not appended as text, so a hand-edited file survives.
    if [ -n "$SHOPS" ] || [ -n "$MARKET" ]; then
        SHOPS="$SHOPS" MARKET="$MARKET" LOCAL_CURRENCY="$LOCAL_CURRENCY" \
        FX_PAIRS="$FX_PAIRS" CFG="$APP_DIR/sync/config.json" python3 - <<'PY'
import json, os

path = os.environ['CFG']
cfg = json.load(open(path, encoding='utf-8'))

if os.environ.get('MARKET'):
    cfg['market'] = os.environ['MARKET']

shops = [s.strip() for s in os.environ.get('SHOPS', '').split(',') if s.strip()]
if shops:
    block = {'shops': {s: {'folder': s} for s in shops}}
    rates = {}
    for pair in os.environ.get('FX_PAIRS', '').split():
        cur, _, rate = pair.partition('=')
        try:
            rates[cur] = float(rate)
        except ValueError:
            pass
    if rates:
        block['local_currency'] = os.environ.get('LOCAL_CURRENCY') or 'PLN'
        block['fx_rates'] = rates
        block['fx_tolerance_pct'] = 5
    cfg['order_matching'] = block

json.dump(cfg, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
open(path, 'a', encoding='utf-8').write('\n')
PY
        echo "  sync/config.json: order matching and market pack filled in"
    fi
fi

# The calculator's rates live in the database, which the app creates on first
# run — so build it here and seed them, rather than making the user retype
# numbers they already gave us.
if [ -n "$B2B_VAT" ] || [ -n "$B2B_TAX" ]; then
    B2B_VAT="$B2B_VAT" B2B_TAX="$B2B_TAX" APP_DIR="$APP_DIR" \
    "$APP_DIR/venv/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, os.environ['APP_DIR'])
import migrations, sqlite3

db_path = os.path.join(os.environ['APP_DIR'], 'budget.db')
migrations.migrate(db_path)
db = sqlite3.connect(db_path)
rows = [('calc.enabled', '1')]
for env_key, setting in (('B2B_VAT', 'calc.vat_rate'), ('B2B_TAX', 'calc.tax_rate')):
    value = os.environ.get(env_key, '').strip()
    if value:
        rows.append((setting, str(float(value) / 100)))
db.executemany('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', rows)
db.commit()
db.close()
PY
    echo "  calculator seeded with your VAT and income tax rates"
fi

# ── 5. service ────────────────────────────────────────────────────────────────

say "Starting"

if command -v systemctl >/dev/null && [ -d /etc/systemd/system ]; then
    RENDER_DIR="$APP_DIR/deploy/rendered"
    mkdir -p "$RENDER_DIR"

    # Space the retries between the daily run and the end of the day, so a
    # morning failure still gets a few chances before midnight.
    WATCHDOG_TIMES=""
    if [ "$SYNC_RUNS" -gt 1 ]; then
        WATCHDOG_TIMES=$(SYNC_TIME="$SYNC_TIME" SYNC_RUNS="$SYNC_RUNS" python3 - <<'PY'
import os
start_h, start_m = (int(x) for x in os.environ['SYNC_TIME'].split(':'))
retries = int(os.environ['SYNC_RUNS']) - 1
# Spread evenly between an hour after the main run and the last slot of the day,
# both included — so there is always one more attempt before midnight.
first, last = min(start_h + 1, 23), 23
if retries == 1:
    times = [last]
else:
    times = [round(first + i * (last - first) / (retries - 1)) for i in range(retries)]
print('\n'.join(f'OnCalendar=*-*-* {h:02d}:{start_m:02d}:00' for h in sorted(set(times))))
PY
)
    fi

    for tpl in "$APP_DIR"/deploy/*.template; do
        out="$RENDER_DIR/$(basename "${tpl%.template}")"
        # Skip the watchdog entirely when the user asked for a single daily run.
        if [ "$SYNC_RUNS" = "1" ] && [[ "$out" == *watchdog* ]]; then continue; fi
        sed -e "s|{{USER}}|$USER|g" -e "s|{{DIR}}|$APP_DIR|g" -e "s|{{PORT}}|$PORT|g" \
            -e "s|{{HOSTNAME}}|$(hostname)|g" -e "s|{{SYNC_TIME}}|$SYNC_TIME|g" "$tpl" \
            | awk -v times="$WATCHDOG_TIMES" '{ if ($0 == "{{WATCHDOG_TIMES}}") print times; else print }' \
            > "$out"
    done
    echo "  unit files rendered into deploy/rendered/"
    if [ "$VARIANT" != "1" ]; then
        echo "  sync at $SYNC_TIME, $SYNC_RUNS attempt(s) per day"
    fi
    echo
    echo "  Installing them needs root, so run these yourself:"
    echo
    echo "    sudo cp $RENDER_DIR/money-badger.service /etc/systemd/system/"
    echo "    sudo systemctl daemon-reload"
    echo "    sudo systemctl enable --now money-badger"
    echo
    echo "  The sync timers in that directory are optional — install the ones you want"
    echo "  the same way. deploy/rendered/nginx.conf is a starting point for a reverse proxy."
else
    echo "  No systemd here. Start the app with:"
    echo
    echo "    $APP_DIR/venv/bin/gunicorn --bind 127.0.0.1:$PORT --workers 1 app:app"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1)
say "Done — open http://${IP:-127.0.0.1}:$PORT/setup to choose your categories and accounts."
[ -n "$PASSWORD" ] && echo "Sign in as $ADMIN_USER."

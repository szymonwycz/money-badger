# Money Badger — Design System

Personal budget app for family use. Available on iPhone and MacBook via LAN.  
Dark theme. Tabs: EXPENSES · INCOME · CHECK ME · TRANSACTIONS · LARGE EXPENSES · OVERVIEW · ROSTER CALCULATIONS.  
Mobile PWA at `/m` — see "Mobile" at the end of this document.  
Character: personal and domestic, but precise like a financial instrument. Not corporate.

---

## Bugs Fixed in This Revision

| Location | Bug | Fix |
|---|---|---|
| `style.css` lines 741–746 | `@media (max-width:760px)` closed at line 740; 5 rules sat outside it globally — `.panel-box { position: static }` killed sticky sidebars, `.col-note { display: none }` always hid the NOTE column, stray `}` at EOF | Moved all rules inside a single `@media` block |
| `tx-table th` | `letter-spacing: 0.6px` — inconsistent with all other uppercase labels | Fixed to `0.8px` |
| `.calc-row` | `flex-wrap: wrap` caused SILNIK/STBY result values to drop to a new line | Changed to `flex-wrap: nowrap`; tightened `calc-input-rate` from 120px → 100px |
| `.le-item-desc` | `font-size: 14px`, no explicit weight or color set | Set to `13px / 400 / var(--text)` matching leaf-row |
| `.le-item.done .le-item-amount` | Color was `var(--text2)` (muted) | Fixed to `var(--green)` — completed expense shown as committed spend |
| `.sum-val` | `font-weight: 700` — same as labels | Changed to `600` — labels own 700, values own 600 |

---

## Signature Element

**Yellow top border on the active navigation tab.**

```css
.nav-link.active {
  border-top-color: var(--yellow);
  border-top-width: 2px;
}
```

Every other app uses bottom underlines or background fills. Home Badger uses a top accent — it reads as "the tab you are on sits above the surface below it." The yellow `--e8b84b` is the same tone used for budget amounts, creating a coherent accent language: yellow = the thing that matters right now. Subtle on desktop, clearly readable on iPhone.

---

## Token System

Source file: `static/design_tokens.css`  
Imported at the top of `style.css` via `@import url('./design_tokens.css')`.

### Colors

| Token | Value | Usage |
|---|---|---|
| `--bg` | `#1a2535` | Main background, table header bg |
| `--bg2` | `#1e2d42` | Secondary bg, hover fills, le-items container |
| `--panel` | `#243048` | Card/panel surface, topnav active tab |
| `--border` | `#2e3d55` | All borders, dividers, hover fill for headers |
| `--text` | `#ffffff` | Primary text, group names, values |
| `--text2` | `#8b9ab0` | Muted text, labels, secondary info |
| `--yellow` | `#e8b84b` | Amounts, ADDITIONAL values, accents, buttons |
| `--blue` | `#0a84ff` | Action buttons, transfers, focus ring |
| `--red` | `#ff453a` | Errors, overbudget, negative REMAINING |
| `--green` | `#30d158` | Income, positive balance, done LE amounts |
| `--teal` | `#2ec4b6` | Asset account values (new — between blue and green) |

### Spacing Scale

| Token | Value | Used for |
|---|---|---|
| `--space-xs` | `4px` | Nav gap |
| `--space-sm` | `8px` | Button padding, input padding, item gaps |
| `--space-md` | `12px` | Panel header margin, section spacing |
| `--space-lg` | `16px` | Page gutter, button horizontal padding |
| `--space-xl` | `24px` | Larger section gaps |
| `--space-xxl` | `32px` | Reserved for major section breaks |

### Border Radius Scale

| Token | Value | Used on |
|---|---|---|
| `--radius-sm` | `4px` | Small inline elements (toggle badges, select inputs in tables, close-btn) |
| `--radius-md` | `8px` | Buttons, text inputs, month-nav buttons |
| `--radius-lg` | `12px` | Cards, panels, balance bar, le-year-header, apply bar |
| `--radius-xl` | `16px` | Modals |

### Font Size Scale

| Token | Value | Used for |
|---|---|---|
| `--fs-xs` | `10px` | Uppercase labels (column headers, sum-label, panel-title, balance-bar-label) |
| `--fs-sm` | `11px` | Badges, secondary labels (acct-type, section-label, le-toggle, balance-name) |
| `--fs-base` | `13px` | Body text, table rows, descriptions, notes |
| `--fs-md` | `14px` | Values, interactive elements, buttons |
| `--fs-lg` | `16px` | Summary bar values (sum-val), modal titles |
| `--fs-xl` | `19px` | Month navigation label |

**18px** (not tokenized): `le-year-label`, `le-title` — these are page-level headers with their own identity, intentionally between `--fs-lg` and `--fs-xl`.

### Shadow

| Token | Value | Used on |
|---|---|---|
| `--shadow-sm` | `0 2px 8px rgba(0,0,0,0.25)` | Panels, balance bar, cards, modals, apply bar |

---

## Typography Rules

| Role | font-size | font-weight | letter-spacing | color |
|---|---|---|---|---|
| Uppercase label | `--fs-xs` (10px) | 700 | 0.8px | `--text2` |
| Section label | `--fs-sm` (11px) | 700 | 0.8px | `--text2` |
| Body / description | `--fs-base` (13px) | 400 | none | `--text2` or `--text` |
| Table value | `--fs-base` (13px) | 400–600 | none | varies by type |
| Interactive value | `--fs-md` (14px) | 600 | none | `--yellow` / `--green` |
| Summary value | `--fs-lg` (16px) | 600 | none | `--yellow` / `--green` / `--red` |
| Month label | `--fs-xl` (19px) | 700 | 1.5px | `--text` |

**Rule:** `letter-spacing` only on UPPERCASE labels (`0.8px`). Never on numeric values — it makes amounts harder to scan.

---

## Component Specs

### TOPNAV

```
Height: ~38px (padding: 8px top, 8px bottom + border)
Font: --fs-xs (10px), 700, letter-spacing 0.8px
Color (inactive): --text2
Color (active): --text
Padding: 7px 14px 8px (active) / 8px 14px (inactive)
Border-radius: --radius-sm --radius-sm 0 0
Active border: 1px --border all sides except bottom; top = 2px --yellow
Hover: color --text
Transition: color 0.15s, border-top-color 0.15s
```

**Signature:** `border-top: 2px solid var(--yellow)` on `.nav-link.active`. The 1px extra is compensated by reducing padding-top from 8px to 7px.

---

### SUMMARY-BAR

```
Background: --panel
Border: 1px --border, top: none
Border-radius: 0 0 --radius-lg --radius-lg
Padding: --space-sm 20px
Margin-bottom: --space-lg
Shadow: --shadow-sm

Sum block:
  Padding: 0 --space-lg (16px each side)
  Gap between label and value: 2px

  .sum-label: --fs-xs, 700, 0.8px spacing, --text2
  .sum-val:   --fs-lg (16px), 600, --yellow (default) / --green (income) / --red (cost)

Divider: 1px --border, full height (align-self: stretch)
Narrative block: --fs-base, --text2, padding-left 20px
```

---

### BALANCE-BAR

Single bar. All accounts in one row. Two value colors:

- Bank/cash accounts → `.balance-val` → `--green`
- Asset accounts → `.balance-val` + `.balance-val-teal` → `--teal`

```
Background: --panel
Border: 1px --border
Border-radius: --radius-lg
Padding: --space-sm 14px
Shadow: --shadow-sm
Margin-bottom: 6px

Label (BALANCE):
  --fs-xs, 700, 0.8px, --text2
  padding-right: --space-sm
  border-right: 1px --border

Balance item:
  display: flex, align-items: center, gap: 6px
  padding: 3px 14px
  border-right: 1px --border (last: none)
  border-radius: --radius-sm
  hover: background --border

  .balance-name: --fs-sm (11px), --text2
  .balance-val:  --fs-md (14px), 600, --green
  .balance-val-teal: same but --teal
```

---

### MONTH-NAV

```
Layout: flex, center, gap 20px
Padding: 14px 0 --space-md

Button:
  background: --panel
  border: 1px --border
  border-radius: --radius-md
  padding: 5px --space-lg
  font-size: --fs-md
  hover: background --border

#month-label:
  font-size: --fs-xl (19px)
  font-weight: 700
  letter-spacing: 1.5px
  min-width: 210px
  text-align: center
```

---

### CAT-TABLE (Budget table)

**Column widths:**

| Class | Width | Align | Notes |
|---|---|---|---|
| `.col-cat` | auto, min 160px | left | KATEGORIA |
| `.col-basic` | 90px | right | BASIC |
| `.col-add` | 100px | right | ADDITIONAL |
| `.col-note` | 160px | left | NOTE |
| `.col-total` | 105px | right | TOTAL |
| `.col-spent` | 90px | right | SPENT |
| `.col-remaining` | 90px | right | REMAINING |

**Headers:**
```
--fs-xs (10px), 700, 0.8px letter-spacing, --text2
border-bottom: 2px --border
position: sticky top:0, z-index:5, background --bg
```

**GROUP-ROW:**
```
Background: --panel
Padding: --space-sm 10px 6px; first-child padding-left --space-md
Border-radius: --radius-md 0 0 (first) / 0 --radius-md 0 (last)
Hover: background --border
cursor: pointer, user-select: none

.group-name-cell: --fs-md (14px), 700, flex + gap --space-sm
.toggle: --fs-xs (10px), --text2, width 12px
  — only render ▶/▼ when group has children; empty span when leaf-group
.group-total: --yellow, 700, text-align right
.group-spent: --text2, 600, text-align right
```

**LEAF-ROW:**
```
Padding: 6px 10px; first-child: 28px (indent)
border-bottom: 1px rgba(46,61,85,0.5)
vertical-align: top
hover: background --bg2, cursor pointer

.leaf-name:  --fs-base (13px), --text2, nowrap + ellipsis
.basic-val:  --fs-base, --text2, right
.add-val:    --fs-base, --yellow, 600, right
.note-val:   12px, --text2, padding-left --space-sm
.total-val:  --fs-base, --text, 600, right
.spent-val:  --fs-base, --green, right
.remaining-val: --fs-base, right
  positive → --green
  negative → --red (add class .negative)
```

**LEAF-GROUP-ROW** (top-level category without children):
```
Same padding as leaf-row
.leaf-group-name: --fs-base, 600, --text, nowrap
```

---

### TX-TABLE (Transactions)

```
font-size: --fs-base (13px)

Headers:
  --fs-xs (10px), 700, letter-spacing 0.8px, --text2
  sticky top:0, background --bg
  border-bottom: 2px --border
  white-space: nowrap

Rows:
  padding: 7px 10px
  border-bottom: 1px rgba(46,61,85,0.4)
  vertical-align: middle
  hover: background --bg2

Column classes:
  .tx-date:   --text2, white-space nowrap
  .tx-amount: --yellow, 600, nowrap, text-align right
  .tx-acct:   --text2, nowrap
  .tx-desc:   max-width 280px, word-wrap break-word (wraps!)
  .tx-cat:    --text2, nowrap
  .tx-subcat: --text2, nowrap, max-width 120px, overflow hidden, text-overflow ellipsis (truncates, does NOT wrap)

Transfer rows (.tx-transfer):
  td: --text2, italic
  .tx-amount: --blue
```

---

### LE-ITEM (Large Expenses page)

```
Grid: 20px 1fr auto auto auto
align-items: center
gap: --space-sm
padding: --space-sm 18px
border-bottom: 1px --border
hover: background --panel

.le-item-desc:
  --fs-base (13px), 400, --text
  (same weight+color as leaf-row — NOT bold)

done state:
  .le-item-desc: text-decoration line-through, color --text2
  .le-item-amount: color --green (expense fulfilled = positive signal)

.le-item-amount: --fs-md (14px), 600, --yellow, nowrap

Inline edit:
  .le-item-desc-input: transparent border at rest
  :focus → border 1px --border, background --bg2
```

---

### KALKULATOR

```
.calc-row:
  flex-wrap: NOWRAP (was wrap — caused SILNIK/STBY to overflow)
  align-items: center
  gap: --space-sm

  .calc-lbl: --fs-base, --text2, width 160px, flex-shrink 0
  .calc-input-sm: 80px, flex-shrink 0
  .calc-input-rate: 100px, flex-shrink 0 (tightened from 120px)
  .calc-sep: --fs-base, --text2, nowrap, flex-shrink 0
  .calc-result: --fs-md, 700, --yellow, min-width 80px, text-align right, margin-left auto, flex-shrink 0
  — always stays inline on the same row

.calc-card: --panel, --radius-lg, --shadow-sm, border 1px --border
.calc-card-title: --fs-sm, 700, 0.8px, --text2

.calc-apply-bar: --panel, --radius-lg, --shadow-sm, flex, align-items center, gap --space-lg
```

---

### MODAL

```
Overlay: rgba(0,0,0,0.65), fixed inset 0, z-index 200
.modal: --panel, --radius-xl (16px), padding 22px, max-width 460px, border 1px --border, --shadow-sm
.modal-small: max-width 300px
.modal-header: flex space-between, margin-bottom 18px
  h2: --fs-lg (16px), 700
  .close-btn: --text2 → --text on hover, --radius-sm
.section-label: --fs-sm, 700, 0.8px, --text2
```

---

## Consistent Patterns

### When to use each color for text values

| Situation | Color |
|---|---|
| Budget amount (planned) | `--yellow` |
| Income / positive balance | `--green` |
| Asset account value | `--teal` |
| Transfer | `--blue` |
| Overbudget / error / negative remaining | `--red` |
| Secondary info / muted | `--text2` |
| Primary content | `--text` |

### Font-weight discipline

| What | Weight |
|---|---|
| Uppercase labels, column headers, panel titles | 700 |
| Values, amounts, group totals | 600 |
| Body text, descriptions, leaf names | 400 |

### Letter-spacing discipline

Only on uppercase labels: `0.8px`. Never on numeric values. Exception: `#month-label` uses `1.5px` because it is decorative display text, not a label.

---

## File Map

```
money-badger/
  static/
    design_tokens.css   ← CSS custom properties (source of truth)
    style.css           ← imports design_tokens.css, all component styles
  design_system.md      ← this file (spec for implementing agents)
```

All other CSS should import `design_tokens.css` before any other rules:
```css
@import url('./design_tokens.css');
```


---

## Shared frontend helpers — `static/common.js`

Loaded before every page script, desktop and mobile. **Do not re-declare these in a
page file**; that is how two `esc()` copies lost their quote-escaping and the money
formatter ended up with three different behaviours on `null`.

| Helper | Purpose |
|---|---|
| `fmtNum(n)` | money, always 2 decimals — a right-aligned column only lines up if every row has the same number of decimals |
| `fmtAmountInput(n)` | value for an `<input type="number">`; keeps float noise (`1234.5600000000001`) out of editable fields |
| `esc(s)` | HTML escape **including `"`** — output is interpolated into attributes |
| `formatDate(s)` | dd/mm/yyyy from any of the three stored formats |
| `amountColor(tx)` | direction colour; reads `tx_type` + category side, never the sign |
| `apiFetch(url, opts)` | fetch + `r.ok` check + error toast. `api()` aliases it; `mobile.js` overrides `api()` for its offline queue |
| `toast(msg, kind)` | transient confirmation / error |
| `clickable(el, fn, label)` | makes a `<tr>`/`<div>` keyboard-operable (tabindex, role, Enter/Space) |
| `shiftMonth(state, ±1)` | month navigation with year wrap |
| `renderSummary(state)` | the summary bar — one implementation for EXPENSES and TRANSACTIONS |
| `renderBalanceBars(accounts)` | the accounts / assets bars |

`MONTHS` is a `const` in common.js. Re-declaring it in a page script is a
**SyntaxError** that takes the whole page down.

Static assets are versioned via `?v={{ v }}` (mtime of `static/`, injected by a Flask
context processor). Keep it on every new `<script>`/`<link>`.

## Mobile PWA (`/m`)

`templates/mobile.html` carries its own stylesheet and does not use the desktop type,
radius or spacing scales — it shares only `design_tokens.css` colours. Tabs:
**ADD · BALANCES · TXNS · BUDGET · CHECK**.

- Minimum tap target **44×44px**.
- Amounts: `white-space: nowrap; flex-shrink: 0` next to any elastic text, or a long
  category name breaks the number in half.
- Writes go through the offline queue in `mobile.js`; a write the server rejects lands
  in `mb-failed-writes` and shows a red badge — it is never silently dropped.

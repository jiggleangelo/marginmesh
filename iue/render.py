import html
import re
from urllib.parse import quote, urlparse

from .econ import InputError, blended_price, break_even, entry_band, number, percentage_or_not_viable

BANNER = 'Demo operated by AI agents. Made-up data. Do not enter real personal data. Nothing is sold.'
HONESTY = 'R is a modelled assumption. Real throughput depends on the serving stack, batch size, sequence lengths and quantisation. Treat outputs as directional.'
CSS = 'body{font:16px system-ui;max-width:1180px;margin:auto;padding:0 18px;color:#172033}a{color:#075cca}.banner{background:#ffe8a3;padding:12px;margin:0 -18px 20px;font-weight:700}nav{display:flex;gap:18px}table{border-collapse:collapse;width:100%;margin:15px 0}th,td{border:1px solid #ccd3dd;padding:7px;text-align:left}th{background:#eef2f7}.cheap{background:#dff6df}.free{background:#fff4cc}.error{color:#9c1625}.card{border:1px solid #ccd3dd;padding:14px;margin:14px 0}label{display:inline-block;margin:5px}input,select,button{padding:7px}.muted{color:#596273}'


def esc(value):
    return html.escape(str(value), quote=True)


def money(value):
    return 'n/a' if value is None else f'${value:.3f}'


def slug(model_id):
    value = re.sub(r'[^a-z0-9]+', '-', model_id.lower()).strip('-')
    return value or 'model'


def join_url(base, path):
    path = path.lstrip('/')
    return f'{base.rstrip("/")}/{path}' if base else f'/{path}'


def _web_url(value):
    if not value:
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in ('http', 'https') and bool(parsed.netloc) else None


def _csp(form_action=None):
    actions = ["'self'"]
    form_action = _web_url(form_action)
    if form_action:
        parsed = urlparse(form_action)
        actions.append(f'{parsed.scheme}://{parsed.netloc}')
    return "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; form-action " + ' '.join(actions) + "; frame-ancestors 'none'; base-uri 'none'"


def layout(title, body, base='', demo=True, canonical_path='/', static=False,
           nav_base=None, form_action=None):
    banner = f'<div class="banner">{BANNER}</div>' if demo else '<div class="banner">Directional estimates · not investment advice</div>'
    nav_base = base if nav_base is None else nav_base
    if static:
        dashboard = join_url(nav_base, 'index.html') if nav_base else 'index.html'
        methodology = join_url(nav_base, 'methodology.html') if nav_base else 'methodology.html'
        export = join_url(nav_base, 'export.csv') if nav_base else 'export.csv'
    else:
        dashboard = join_url(nav_base, '')
        methodology = join_url(nav_base, 'methodology')
        export = join_url(nav_base, 'export.csv')
    canonical = ''
    if base and urlparse(base).scheme:
        canonical = f'<link rel="canonical" href="{esc(base.rstrip("/") + canonical_path)}">'
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<meta http-equiv="Content-Security-Policy" content="{esc(_csp(form_action))}">'
            f'<title>{esc(title)}</title><style>{CSS}</style>{canonical}</head><body>{banner}'
            f'<nav><a href="{esc(dashboard)}">Dashboard</a><a href="{esc(methodology)}">Methodology</a>'
            f'<a href="{esc(export)}">Dashboard CSV</a></nav>{body}</body></html>')


def dashboard_page(reports, base='', static=False, demo=True, form_action=None):
    rows = []
    for report in reports:
        target = f'model/{quote(slug(report.model_id))}.html' if static else f'model?id={quote(report.model_id)}'
        spread = 'n/a' if report.spread_ratio is None else f'{report.spread_ratio:.1f}×'
        href = join_url(base, target) if base else target if static else join_url('', target)
        rows.append(
            f'<tr><td><a href="{esc(href)}">{esc(report.model_id)}</a></td>'
            f'<td>{report.provider_count}</td><td>{report.free_count}</td><td>{money(report.price_min)}</td><td>{money(report.price_median)}</td>'
            f'<td>{money(report.price_max)}</td><td>{spread}</td><td>{esc(report.quant_flag)}</td>'
            f'<td>{esc(report.uptime_flag)}</td><td>{"yes" if report.stale_flag else "no"}</td></tr>'
        )
    body = ('<h1>Inference Unit Economics</h1><div class="card"><b>Start here:</b> scan the made-up market, open a sample model, then change the fictional rent and utilisation assumptions.</div>'
            '<table><tr><th>Model</th><th>Paid providers</th><th>Free endpoints</th><th>Min</th><th>Median</th><th>Max</th><th>Spread</th><th>Quant</th><th>Uptime</th><th>Stale</th></tr>' + ''.join(rows) + '</table>')
    options = ''.join(f'<option value="{esc(report.model_id)}">{esc(report.model_id)} — made-up sample</option>' for report in reports)
    if not static:
        action_request = join_url(base, 'request')
        action_signup = join_url(base, 'signup')
        body += ('<h2>Try sample interest signals</h2>'
                 '<p class="muted">These controls use only the clearly labelled made-up sample choices shown here. Do not enter real personal data. '
                 'Sample choices are one-way hashed, cannot be read back, and cannot be used to contact you. Only aggregate submission counts are usable. '
                 'The demo keeps at most 1,000 count-only records for up to 24 hours and deletes them on restart. A receipt permits immediate deletion.</p>'
                 f'<form method="post" action="{esc(action_request)}"><label>Made-up sample model <select name="sample_model" required>{options}</select></label><button>Count sample request</button></form>'
                 f'<form method="post" action="{esc(action_signup)}"><button name="sample_interest" value="made-up-demo-signal">Count sample interest</button></form>')
    else:
        safe_action = _web_url(form_action)
        if safe_action:
            body += ('<h2>Stay informed or request a model</h2>'
                     '<p class="muted">These forms submit to the configured external form service. Its privacy terms and retention policy apply.</p>'
                     f'<form method="post" action="{esc(safe_action)}"><input type="hidden" name="intent" value="signup">'
                     '<label>Email <input type="email" name="email" required></label><button>Join updates</button></form>'
                     f'<form method="post" action="{esc(safe_action)}"><input type="hidden" name="intent" value="model_request">'
                     f'<label>Requested model <select name="model_name" required>{options}</select></label><button>Request this model</button></form>')
    return layout('IUE dashboard', body, base, demo, '/', static=static, form_action=form_action)


def _value(name, scenario, form_values, default):
    if form_values and name in form_values:
        return form_values[name]
    if scenario and name in scenario:
        return scenario[name]
    return default


def _number_value(value, digits=3):
    return '' if value is None else f'{value:.{digits}f}' if isinstance(value, (int, float)) else str(value)


def _percentage(value):
    return 'no market price' if value is None else percentage_or_not_viable(value)


def _chip_economics(report):
    rows = []
    for chip in report.chips:
        if chip['rent'] is None or report.price_min is None or report.price_median is None:
            at_min = at_median = 'n/a'
        else:
            try:
                at_min = _percentage(break_even(chip['rent'], chip['r'], report.price_min))
                at_median = _percentage(break_even(chip['rent'], chip['r'], report.price_median))
            except (InputError, ArithmeticError):
                at_min = at_median = 'invalid assumption'
        if chip['rent'] is None:
            entry_low = entry_high = 'n/a'
        else:
            try:
                low, high = entry_band(chip['rent'], chip['r'])
                entry_low, entry_high = money(low), money(high)
            except (InputError, ArithmeticError):
                entry_low = entry_high = 'invalid assumption'
        rows.append(f'<tr><td>{esc(chip["chip"])}</td><td>{money(chip["rent"])}</td><td>{chip["r"]:.3f}</td><td>{esc(at_min)}</td><td>{esc(at_median)}</td><td>{esc(entry_low)}</td><td>{esc(entry_high)}</td></tr>')
    return ''.join(rows) or '<tr><td colspan="7">no serving assumption</td></tr>'


def model_page(report, scenario, error=None, base='', static=False, demo=True,
               embedded_json='', form_values=None):
    k_value = _value('k', scenario, form_values, 3)
    try:
        k_for_table = number('k', k_value, low=0, high=20)
    except InputError:
        k_for_table = 3
    paid_prices = [blended_price(x.price_in, x.price_out, k_for_table) for x in report.endpoints if x.price_in or x.price_out]
    cheapest = min(paid_prices, default=None)
    provider_rows = []
    for endpoint in report.endpoints:
        is_paid = bool(endpoint.price_in or endpoint.price_out)
        price = blended_price(endpoint.price_in, endpoint.price_out, k_for_table) if is_paid else None
        row_class = 'cheap' if is_paid and price == cheapest else 'free' if not is_paid else ''
        status = 'paid' if is_paid else 'free — excluded from market statistics'
        blended = money(price) if is_paid else 'free'
        provider_rows.append(
            f'<tr class="{row_class}"><td>{esc(endpoint.provider)}</td><td>{esc(status)}</td>'
            f'<td>{money(endpoint.price_in)}</td><td>{money(endpoint.price_out)}</td><td>{esc(blended)}</td>'
            f'<td>{esc(endpoint.quantization or "undisclosed")}</td><td>{esc(endpoint.context_len or "n/a")}</td>'
            f'<td>{esc(endpoint.uptime_pct if endpoint.uptime_pct is not None else "n/a")}</td>'
            f'<td>{esc(endpoint.throughput_tps if endpoint.throughput_tps is not None else "n/a")}</td></tr>'
        )
    providers = ''.join(provider_rows) or '<tr><td colspan="9">no endpoints</td></tr>'
    chips = ''.join(
        f'<tr><td>{esc(chip["chip"])}</td><td>{money(chip["rent"])}</td><td>{chip["r"]:.3f}</td><td>{esc(chip["confidence"])}</td></tr>'
        for chip in report.chips
    ) or '<tr><td colspan="4">no serving assumption</td></tr>'
    selected_chip = str(_value('chip', scenario, form_values, report.chips[0]['chip'] if report.chips else ''))
    options = ''.join(
        f'<option value="{esc(chip["chip"])}" {"selected" if chip["chip"] == selected_chip else ""}>{esc(chip["chip"])}</option>'
        for chip in report.chips
    )
    if selected_chip and all(chip['chip'] != selected_chip for chip in report.chips):
        options = f'<option selected value="{esc(selected_chip)}">{esc(selected_chip)}</option>' + options
    err = f'<p class="error">{esc(error)}</p>' if error else ''
    if error:
        result = '<div id="calc-output" class="card">Results unavailable until the invalid values are corrected.</div>'
    elif scenario is None:
        reason = 'no serving assumption' if not report.chips else 'no rent quote'
        result = f'<div id="calc-output" class="card">{reason}</div>'
    else:
        utilisation = f'{scenario["u"]:.0%}'
        margin_summary = ('market margins unavailable' if scenario.get('margin_min') is None else
                          f'margin/GPU-hour at min {money(scenario["margin_min"])} and median {money(scenario["margin_median"])}')
        result = (f'<div id="calc-output" class="card"><b>Current scenario at {utilisation} utilisation:</b> {margin_summary}; '
                  f'target-margin entry price {money(scenario.get("entry_at_u"))}. Break-even at min {_percentage(scenario["break_even_min"])}; '
                  f'at median {_percentage(scenario["break_even_median"])}. Entry band {money(scenario["entry_low"])}–{money(scenario["entry_high"])}. '
                  f'Match utilisation {_percentage(scenario["u_match"])}. <b>{esc(scenario["verdict"])}</b>.</div>')
    form = ''
    if report.chips:
        action = '' if static else join_url(base, 'model')
        hidden = '' if static else f'<input type="hidden" name="id" value="{esc(report.model_id)}">'
        fields = {
            'rent': _value('rent', scenario, form_values, report.chips[0]['rent']),
            'r': _value('r', scenario, form_values, report.chips[0]['r']),
            'u': _value('u', scenario, form_values, .6),
            'g': _value('g', scenario, form_values, .3),
            'k': k_value,
            'ulo': _value('ulo', scenario, form_values, .4),
            'uhi': _value('uhi', scenario, form_values, .7),
        }
        form = (f'<form id="calculator" method="get" action="{esc(action)}">{hidden}<label>Chip <select name="chip">{options}</select></label>'
                f'<label>Rent (made-up median quote) <input name="rent" value="{esc(_number_value(fields["rent"]))}"></label>'
                f'<label>R <input name="r" value="{esc(_number_value(fields["r"]))}"></label>'
                f'<label>Utilisation <input name="u" value="{esc(_number_value(fields["u"], 2))}"></label>'
                f'<label>Margin <input name="g" value="{esc(_number_value(fields["g"], 2))}"></label>'
                f'<label>Input:output ratio <input name="k" value="{esc(_number_value(fields["k"], 2))}"></label>'
                f'<label>Band low <input name="ulo" value="{esc(_number_value(fields["ulo"], 2))}"></label>'
                f'<label>Band high <input name="uhi" value="{esc(_number_value(fields["uhi"], 2))}"></label><button>Recalculate</button></form>')
    csv_link = ''
    if scenario is not None:
        if static:
            csv_target = f'{quote(slug(report.model_id))}.csv'
        else:
            csv_target = (f'model.csv?id={quote(report.model_id)}&chip={quote(str(scenario["chip"]))}&rent={scenario["rent"]}'
                          f'&r={scenario["r"]}&u={scenario["u"]}&g={scenario["g"]}&k={scenario.get("k", 3)}'
                          f'&ulo={scenario["u_lo"]}&uhi={scenario["u_hi"]}')
            csv_target = join_url(base, csv_target)
        csv_link = f'<p><a href="{esc(csv_target)}">Download this scenario (CSV)</a></p>'
    economics = _chip_economics(report)
    body = (f'<h1>{esc(report.model_id)}</h1>{err}<h2>Providers</h2><table><tr><th>Provider</th><th>Status</th><th>Input</th><th>Output</th>'
            f'<th>Blended</th><th>Quant</th><th>Context</th><th>Uptime %</th><th>Throughput</th></tr>{providers}</table>'
            f'<h2>Serving assumptions</h2><table><tr><th>Chip</th><th>Median rent/h</th><th>M tokens/GPU-h</th><th>Confidence</th></tr>{chips}</table>'
            f'<h2>Per-chip break-even</h2><table><tr><th>Chip</th><th>Median rent/h</th><th>M tokens/GPU-h</th><th>At market minimum</th><th>At market median</th><th>Entry low (70% utilisation)</th><th>Entry high (40% utilisation)</th></tr>{economics}</table>'
            f'<p class="muted">Entry bands use a 30% target margin. {HONESTY}</p><h2>Calculator</h2>{form}{result}{csv_link}{embedded_json}')
    canonical_path = f'/model/{quote(slug(report.model_id))}.html' if static else f'/model?id={quote(report.model_id)}'
    return layout(report.model_id, body, base, demo, canonical_path, static=static,
                  nav_base=base if base else '..' if static else '')


def methodology_page(base='', demo=True, static=False):
    source_text = ('Demo sources and values are made up. Endpoint observations are dated 2026-10-21 and 2026-10-25; rent observations are dated 2026-10-25.' if demo else
                   'Live adapters use OpenRouter prices plus verified rent and serving source files.')
    body = ('<h1>Methodology</h1><p>Prices are USD per million tokens. Blended price = (k × input + output)/(k+1). R is million tokens per GPU-hour.</p>'
            '<ul><li>Margin/GPU-hour = p × R × u − c</li><li>Break-even utilisation = c/(p × R)</li>'
            '<li>Entry price = c/(R × u × (1−g))</li><li>Match utilisation = c/(minimum price × R × (1−g))</li></ul>'
            '<p>Free endpoints are displayed and flagged separately, but excluded from market price statistics. Rows older than 48 hours at build time are stale. Rent uses the median quote for a chip.</p>'
            '<p><b>Sensitivity:</b> break-even utilisation scales as 1/R, so a 20% overestimate of R creates roughly a 25% underestimate of break-even utilisation.</p>'
            f'<p>{source_text} Artificial Analysis and Silicon Data are not used. Serving confidence is shown per row.</p><p class="muted">{HONESTY}</p>')
    return layout('Methodology', body, base, demo, '/methodology.html' if static else '/methodology', static=static)


def message_page(title, message, base=''):
    return layout(title, f'<h1>{esc(title)}</h1><p>{esc(message)}</p><p><a href="{esc(join_url(base, ""))}">Return to dashboard</a></p>', base, True)

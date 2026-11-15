import csv
import io

from .econ import percentage_or_not_viable


def _safe(value):
    if value is None:
        return ''
    value = str(value)
    probe = value.lstrip('\t\r')
    return "'" + value if probe.startswith(('=', '+', '-', '@')) else value


def _write(headers, rows):
    out = io.StringIO(newline='')
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow([_safe(value) for value in headers])
    writer.writerows([_safe(value) for value in row] for row in rows)
    return out.getvalue()


def dashboard(reports):
    heads = ['model_id', 'provider_count', 'free_count', 'price_min', 'price_median', 'price_max', 'spread_ratio', 'spread_pct', 'quant_flag', 'uptime_flag', 'stale_flag']
    return _write(heads, ([getattr(report, key) for key in heads] for report in reports))


def _utilisation(value):
    if value is None:
        return 'no market price'
    return percentage_or_not_viable(value) if value > 1 else value


def economics(model_id, scenarios):
    heads = ['model_id', 'chip', 'rent', 'r', 'utilisation', 'target_margin',
             'margin_min', 'margin_median', 'entry_at_utilisation', 'u_lo', 'u_hi',
             'break_even_min', 'break_even_median', 'entry_low', 'entry_high',
             'u_match', 'verdict']
    return _write(heads, (
        [model_id, item['chip'], item['rent'], item['r'], item['u'], item['g'],
         item.get('margin_min'), item.get('margin_median'), item.get('entry_at_u'),
         item['u_lo'], item['u_hi'], _utilisation(item['break_even_min']),
         _utilisation(item['break_even_median']), item['entry_low'], item['entry_high'],
         _utilisation(item['u_match']), item['verdict']]
        for item in scenarios
    ))

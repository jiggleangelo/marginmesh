import csv
import io
import json
import math
from datetime import datetime, timezone

from . import econ
from .openrouter import OpenRouterPrice

PRICE_MAX_ROWS = 1000
CHIP_MAX_ROWS = 100
MAX_BYTES = 1024 * 1024


class InputFileError(ValueError):
    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = (errors or [])[:5]


def _text(value, name, limit=200, required=False):
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError(f'{name} must be text')
    value = str(value or '').strip()
    if required and not value:
        raise ValueError(f'{name} is required')
    if len(value) > limit:
        raise ValueError(f'{name} is longer than {limit} characters')
    return value


def _finite(value, name, low=0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a number')
    if not math.isfinite(value) or value < low:
        raise ValueError(f'{name} must be finite and at least {low}')
    return value


def _integer(value, name, low=0, high=10_000_000):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be an integer')
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'{name} must be an integer')
    if str(value).strip() not in (str(result), f'{result}.0'):
        raise ValueError(f'{name} must be an integer')
    if not low <= result <= high:
        raise ValueError(f'{name} must be an integer from {low} to {high}')
    return result


def _observed(value, now):
    value = _text(value, 'observed_at') or now
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError, OverflowError):
        raise ValueError('observed_at must be an ISO-8601 timestamp')
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('observed_at must include a timezone')
    return value


def _input_text(text):
    if not isinstance(text, str):
        raise InputFileError('Input must be UTF-8 text')
    try:
        size = len(text.encode('utf-8'))
    except UnicodeError:
        raise InputFileError('Input must be UTF-8 text')
    if size > MAX_BYTES:
        raise InputFileError('Input is larger than 1 MB')
    return text.lstrip('\ufeff')


def _reader(text, required):
    text = _input_text(text)
    try:
        reader = csv.DictReader(io.StringIO(text, newline=''))
        names = reader.fieldnames
    except csv.Error as exc:
        raise InputFileError(f'CSV could not be read: {exc}')
    if not names:
        raise InputFileError('CSV needs a header row')
    names = [str(x or '').lstrip('\ufeff').strip().lower() for x in names]
    if len(names) != len(set(names)):
        raise InputFileError('CSV headers must be unique')
    missing = sorted(set(required) - set(names))
    if missing:
        raise InputFileError('Missing required headers: ' + ', '.join(missing))
    reader.fieldnames = names
    return reader


def _finish(rows, bad, errors, total):
    if total and bad * 2 > total:
        raise InputFileError(f'{bad} of {total} rows were invalid', errors)
    return rows, bad, errors[:5]


def _optional_number(value, name):
    return None if value in (None, '') else _finite(value, name)


def _endpoint(model, provider, pin, pout, quant, context, uptime, throughput, observed, now):
    uptime = _optional_number(uptime, 'uptime_pct')
    if uptime is not None and uptime > 100:
        raise ValueError('uptime_pct must be at most 100')
    return {
        'model_id': _text(model, 'model', 80, True),
        'provider': _text(provider, 'provider', 200, True),
        'price_in': _finite(pin, 'price_in'),
        'price_out': _finite(pout, 'price_out'),
        'quantization': _text(quant, 'quant') or None,
        'context_len': None if context in (None, '') else _integer(context, 'context_len'),
        'uptime_pct': uptime,
        'throughput_tps': _optional_number(throughput, 'throughput_tps'),
        'observed_at': _observed(observed, now),
    }


def _csv_rows(reader, limit, convert):
    rows, errors, bad, total = [], [], 0, 0
    try:
        for row in reader:
            total += 1
            if total > limit:
                raise InputFileError(f'Input can contain at most {limit} rows')
            try:
                if None in row:
                    raise ValueError('Row has more fields than the header')
                rows.append(convert(row))
            except (ValueError, TypeError, ArithmeticError) as exc:
                bad += 1
                if len(errors) < 5:
                    errors.append(f'line {reader.line_num}: {exc}')
    except csv.Error as exc:
        raise InputFileError(f'CSV could not be read: {exc}')
    return _finish(rows, bad, errors, total)


def parse_prices_csv(text, unit='per_m'):
    if unit not in ('per_m', 'per_token'):
        raise InputFileError('Unknown price unit')
    reader = _reader(text, ('model', 'provider', 'price_in', 'price_out'))
    multiplier = 1_000_000 if unit == 'per_token' else 1
    now = datetime.now(timezone.utc).isoformat()

    def convert(row):
        return _endpoint(
            row['model'], row['provider'],
            _finite(row['price_in'], 'price_in') * multiplier,
            _finite(row['price_out'], 'price_out') * multiplier,
            row.get('quant'), row.get('context_len'), row.get('uptime_pct'),
            None, row.get('observed_at'), now,
        )

    return _csv_rows(reader, PRICE_MAX_ROWS, convert)


def parse_chips_csv(text):
    reader = _reader(text, ('chip', 'usd_per_gpu_hour'))
    names = set(reader.fieldnames)
    if 'm_tokens_per_gpu_hour' not in names and not {'gpus_per_replica', 'tps_per_replica'} <= names:
        raise InputFileError('Provide m_tokens_per_gpu_hour or both gpus_per_replica and tps_per_replica')

    def convert(row):
        direct = row.get('m_tokens_per_gpu_hour')
        if direct is not None and direct.strip():
            rate = econ.number('R', direct, low=0, low_open=True)
        else:
            gpus = _integer(row.get('gpus_per_replica'), 'gpus_per_replica', low=1)
            rate = econ.rate_million(row.get('tps_per_replica'), gpus)
        rate = econ.number('R', rate, low=0, low_open=True)
        return {
            'chip': _text(row['chip'], 'chip', required=True),
            'rent': econ.number('rent', row['usd_per_gpu_hour'], low=0, low_open=True),
            'r': rate,
            'model': _text(row.get('model'), 'model', 80),
            'quant': _text(row.get('quant'), 'quant'),
            'source': _text(row.get('source'), 'source'),
            'confidence': _text(row.get('confidence'), 'confidence') or 'estimate',
        }

    return _csv_rows(reader, CHIP_MAX_ROWS, convert)


def parse_openrouter_json(text, model):
    text = _input_text(text)
    try:
        model = _text(model, 'model', 80, True)
        payload = json.loads(text)
    except (ValueError, TypeError, RecursionError) as exc:
        raise InputFileError('Invalid JSON or model', [str(exc)])
    if not isinstance(payload, dict):
        raise InputFileError('OpenRouter JSON must be an object')
    data = payload.get('data', {})
    if not isinstance(data, dict):
        raise InputFileError('OpenRouter data must be an object')
    endpoints = data.get('endpoints', payload.get('endpoints'))
    if not isinstance(endpoints, list):
        raise InputFileError('OpenRouter JSON must contain an endpoints array')
    if len(endpoints) > PRICE_MAX_ROWS:
        raise InputFileError(f'Input can contain at most {PRICE_MAX_ROWS} rows')

    rows, errors, bad = [], [], 0
    now = datetime.now(timezone.utc).isoformat()
    for index, raw in enumerate(endpoints, 1):
        try:
            if not isinstance(raw, dict):
                raise ValueError('Endpoint must be an object')
            # Validate the supplied timestamp before the tolerant adapter applies
            # its missing-value fallback. Never persist structured timestamps.
            observed = _observed(raw.get('observed_at'), now)
            document = json.dumps({'data': {'endpoints': [raw]}}).encode('utf-8')
            source = OpenRouterPrice([model], opener=lambda *args, **kwargs: io.BytesIO(document))
            parsed = source.endpoints()
            if len(parsed) != 1:
                raise ValueError('Endpoint prices or fields are invalid')
            endpoint = parsed[0]
            rows.append(_endpoint(
                endpoint.model_id, endpoint.provider, endpoint.price_in,
                endpoint.price_out, endpoint.quantization, endpoint.context_len,
                endpoint.uptime_pct, endpoint.throughput_tps, observed, now,
            ))
        except (ValueError, TypeError, AttributeError, ArithmeticError, RecursionError) as exc:
            bad += 1
            if len(errors) < 5:
                errors.append(f'endpoint {index}: {exc}')
    # Count every original endpoint, including validation failures. A document
    # containing one invalid endpoint has 1/1 invalid rows, not zero rows.
    return _finish(rows, bad, errors, len(endpoints))

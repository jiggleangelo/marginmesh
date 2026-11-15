import math


class InputError(ValueError):
    pass


def number(name, value, *, low=None, high=None, low_open=False, high_open=False):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise InputError(f"{name} must be a number")
    if not math.isfinite(value):
        raise InputError(f"{name} must be finite")
    if low is not None and (value <= low if low_open else value < low):
        raise InputError(f"{name} must be {'greater than' if low_open else 'at least'} {low}")
    if high is not None and (value >= high if high_open else value > high):
        raise InputError(f"{name} must be {'less than' if high_open else 'at most'} {high}")
    return value


def blended_price(price_in, price_out, k=3):
    k = number("input:output ratio", k, low=0, high=20)
    return (k * number("input price", price_in, low=0) + number("output price", price_out, low=0)) / (k + 1)


def rate_million(tps_per_replica, gpus_per_replica):
    tps = number("throughput", tps_per_replica, low=0, low_open=True)
    gpus = number("GPU count", gpus_per_replica, low=0, low_open=True)
    return tps * 3600 / gpus / 1_000_000


def validate(c, r, u, g, k=3):
    return (
        number("rent", c, low=0, low_open=True),
        number("R", r, low=0, low_open=True),
        number("utilisation", u, low=0, high=1, low_open=True),
        number("target margin", g, low=0, high=.95),
        number("input:output ratio", k, low=0, high=20),
    )


def margin(c, r, p, u):
    c, r, u, _, _ = validate(c, r, u, 0)
    return number("price", p, low=0) * r * u - c


def break_even(c, r, p):
    c = number("rent", c, low=0, low_open=True)
    r = number("R", r, low=0, low_open=True)
    p = number("price", p, low=0, low_open=True)
    return c / (p * r)


def entry_price(c, r, u, g):
    c, r, u, g, _ = validate(c, r, u, g)
    return c / (r * u * (1 - g))


def entry_band(c, r, u_lo=.4, u_hi=.7, g=.3):
    lo = number("low utilisation", u_lo, low=0, high=1, low_open=True)
    hi = number("high utilisation", u_hi, low=0, high=1, low_open=True)
    if lo > hi:
        raise InputError("low utilisation must not exceed high utilisation")
    return entry_price(c, r, hi, g), entry_price(c, r, lo, g)


def match_utilisation(c, r, p_min, g):
    g = number("target margin", g, low=0, high=.95)
    return break_even(c, r, p_min) / (1 - g)


def verdict(entry_low, p_min, p_median):
    if entry_low < p_min:
        return "room to undercut"
    if entry_low <= p_median:
        return "competitive at median"
    return "priced out"


def percentage_or_not_viable(value):
    return "not viable on this chip at this price" if value > 1 else f"{value:.0%}"

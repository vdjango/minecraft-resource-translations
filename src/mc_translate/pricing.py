"""Versioned standard GPT text prices, USD per million tokens.

Reference costs are not a reseller invoice. Unknown model aliases are never
guessed. Store the rate snapshot with each call so changing settings cannot
rewrite previously recorded costs.
"""
import re

PRICE_DATE = '2026-11-01'
PRICE_SOURCE = 'https://developers.openai.com/api/docs/pricing'
OFFICIAL_PRICES = {
    'gpt-6-astra': dict(input=10., cached_input=1., output=50., cache_write_30m=12.5, cache_write_12h=25.),
    'gpt-6.1-sol': dict(input=2., cached_input=.1, output=10., cache_write_30m=2.5, cache_write_12h=5.),
    'gpt-6.1-terra': dict(input=1., cached_input=.05, output=5., cache_write_30m=1.25, cache_write_12h=2.5),
    'gpt-6-luna': dict(input=.1, cached_input=.01, output=.5, cache_write_30m=.125, cache_write_12h=.25),
    'gpt-6-sol': dict(input=2., cached_input=.2, output=10., cache_write_30m=2.5, cache_write_12h=5.),
    'gpt-4.1': dict(input=2., cached_input=.5, output=8.),
    'gpt-4.1-mini': dict(input=.4, cached_input=.1, output=1.6),
}


def rate(model, overrides=None):
    if model in (overrides or {}):
        return {**overrides[model], 'basis': 'custom', 'currency': 'USD'}
    key = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', model or '')
    if key not in OFFICIAL_PRICES:
        return None
    return {**OFFICIAL_PRICES[key], 'basis': 'official', 'model': key,
            'date': PRICE_DATE, 'source': PRICE_SOURCE, 'currency': 'USD'}


def calculate(usage, model, overrides=None):
    """Accept Chat Completions and Responses usage, without counting reasoning twice."""
    valid = lambda value: type(value) is int and value >= 0
    incoming = usage.get('prompt_tokens', usage.get('input_tokens'))
    outgoing = usage.get('completion_tokens', usage.get('output_tokens'))
    total = usage.get('total_tokens')
    if total is None and valid(incoming) and valid(outgoing):
        total = incoming + outgoing
    result = dict(total=total if valid(total) else None, input=0, output=0, cached=0,
                  written=0, details_known=False, cost=None, snapshot=None, basis='unknown')
    if not (valid(incoming) and valid(outgoing) and valid(total) and incoming + outgoing == total):
        return result
    details = usage.get('prompt_tokens_details', usage.get('input_tokens_details')) or {}
    if not isinstance(details, dict):
        return result
    cached = details.get('cached_tokens', 0)
    written = details.get('cache_write_tokens', 0)
    splits = details.get('cache_write_tokens_details') or {}
    if not isinstance(splits, dict):
        return result
    write30, write12 = splits.get('ttl_30m_tokens', written), splits.get('ttl_12h_tokens', 0)
    if not all(valid(v) for v in (cached, written, write30, write12)):
        return result
    if cached + written > incoming or write30 + write12 != written:
        return result
    result.update(input=incoming, output=outgoing, cached=cached, written=written, details_known=True)
    price = rate(model, overrides)
    if not price:
        return result
    # Standard requests only. A supplier that omits cache details yields a
    # clearly labelled reference estimate using ordinary input tokens.
    estimated = 'cached_tokens' not in details or (written > 0 and not splits)
    long = price.get('basis') == 'official' and price.get('model') in ('gpt-6.1-sol', 'gpt-6-sol') and incoming > 272000
    im, om = (2, 1.5) if long else (1, 1)
    cost = ((incoming-cached-written)*price['input'] + cached*price['cached_input']
            + write30*price.get('cache_write_30m', price['input'])
            + write12*price.get('cache_write_12h', price['input'])) * im + outgoing*price['output']*om
    result.update(cost=cost/1_000_000, basis=price['basis'] + ('_estimate' if estimated else ''),
                  snapshot={**price, 'long_context': bool(long), 'estimated': estimated})
    return result

"""IBAN normalization shared by the pipeline.

config.json stores IBANs however the user typed them; Enable Banking wants the
full form with the country code. These two functions are the only place that
knows the difference, and neither assumes a country.
"""
import re

_COUNTRY_RE = re.compile(r'^[A-Z]{2}\d')


def bare_iban(iban: str) -> str:
    """Strip spaces and the country prefix — the key config.json is written with."""
    s = (iban or '').replace(' ', '').strip().upper()
    return s[2:] if _COUNTRY_RE.match(s) else s


def full_iban(iban: str, country: str = '') -> str:
    """Add the country prefix back if it isn't already there.

    country comes from config.json (enable_banking.default_aspsp.country, or the
    per-IBAN entry). Without one the IBAN is returned as given, which is correct
    when config.json already stores full IBANs.
    """
    s = (iban or '').replace(' ', '').strip().upper()
    if _COUNTRY_RE.match(s) or not country:
        return s
    return f'{country.upper()}{s}'

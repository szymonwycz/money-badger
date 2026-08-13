"""IBAN normalization — the country prefix used to be the literal 'PL'.

The IBANs are made up — only their shape matters here.
"""
import iban


def test_bare_strips_country_and_spaces():
    assert iban.bare_iban('PL00 0000 0000 0000 0000 0000 0001') == '00000000000000000000000001'
    assert iban.bare_iban('DE00000000000000000002') == '00000000000000000002'
    assert iban.bare_iban('00000000000000000000000001') == '00000000000000000000000001'


def test_bare_is_idempotent():
    once = iban.bare_iban('PL00000000000000000000000001')
    assert iban.bare_iban(once) == once


def test_a_bare_iban_starting_with_letters_is_not_stripped_twice():
    """Only a real country prefix (two letters then a digit) is removed."""
    assert iban.bare_iban('AB12345') == '12345'
    assert iban.bare_iban('ABCDEF') == 'ABCDEF'


def test_full_adds_the_configured_country():
    assert iban.full_iban('00000000000000000000000001', 'PL') == 'PL00000000000000000000000001'
    assert iban.full_iban('00000000000000000002', 'de') == 'DE00000000000000000002'


def test_full_leaves_an_already_qualified_iban_alone():
    assert iban.full_iban('PL00000000000000000000000001', 'DE') == 'PL00000000000000000000000001'


def test_full_without_a_country_returns_what_it_was_given():
    """Correct when config.json already stores full IBANs."""
    assert iban.full_iban('00000000000000000000000001') == '00000000000000000000000001'


def test_round_trip():
    for value, country in (('PL00000000000000000000000001', 'PL'),
                           ('DE00000000000000000002', 'DE')):
        assert iban.full_iban(iban.bare_iban(value), country) == value

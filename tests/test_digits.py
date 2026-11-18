from decimal import Decimal
import pytest
from bromax.digits import extract_last_digit, quote_precision, QuotePrecisionError

@pytest.mark.parametrize("symbol,quote,pip_size,expected_text,expected_digit",[
    ("1HZ100V","1234.50",2,"1234.50",0),
    ("1HZ100V",Decimal("1234.5"),2,"1234.50",0),
    ("1HZ100V","1234.56",2,"1234.56",6),
    ("1HZ15V","987.1",3,"987.100",0),
    ("R_100","987.123",3,"987.123",3),
    ("R_50","100.00",2,"100.00",0),
    ("R_10","42",2,"42.00",0),
])
def test_last_displayed_digit_by_symbol_precision(symbol,quote,pip_size,expected_text,expected_digit):
    assert extract_last_digit(quote,pip_size)==(expected_text,expected_digit)

def test_zero_decimal_precision_has_no_decimal_digit():
    assert extract_last_digit("1234",0)==("1234",None)

def test_rejects_nonzero_excess_precision():
    with pytest.raises(QuotePrecisionError): extract_last_digit("1.234",2)

def test_rejects_bad_precision():
    with pytest.raises(QuotePrecisionError): quote_precision("2.5")


def test_binary_float_quotes_are_rejected():
    with pytest.raises(QuotePrecisionError): extract_last_digit(1234.5,2)

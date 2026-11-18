from decimal import Decimal, InvalidOperation

class QuotePrecisionError(ValueError):
    pass

def quote_precision(pip_size: int | str | Decimal) -> int:
    """Deriv tick.pip_size is the number of decimal places to display (toFixed precision)."""
    try:
        d = Decimal(str(pip_size))
    except (InvalidOperation, ValueError) as exc:
        raise QuotePrecisionError(f"Invalid pip_size: {pip_size!r}") from exc
    if not d.is_finite() or d != d.to_integral_value() or not 0 <= int(d) <= 10:
        raise QuotePrecisionError(f"pip_size must be an integer precision from 0 to 10, got {pip_size!r}")
    return int(d)

def extract_last_digit(quote: str | int | Decimal, pip_size: int | str | Decimal) -> tuple[str, int | None]:
    """Format quote to Deriv's symbol precision, then take the final displayed decimal digit.

    No float conversion is used. Trailing zeroes are restored from pip_size, not
    inferred from the JSON number's lexical representation.
    """
    precision = quote_precision(pip_size)
    if isinstance(quote, float):
        raise QuotePrecisionError("Binary float quotes are not accepted; parse the raw JSON number as Decimal")
    try:
        value = quote if isinstance(quote, Decimal) else Decimal(str(quote))
    except (InvalidOperation, ValueError) as exc:
        raise QuotePrecisionError(f"Malformed quote: {quote!r}") from exc
    if not value.is_finite():
        raise QuotePrecisionError(f"Non-finite quote: {quote!r}")
    quantum = Decimal(1).scaleb(-precision)
    try:
        displayed = value.quantize(quantum)
    except InvalidOperation as exc:
        raise QuotePrecisionError(f"Quote cannot be represented at precision {precision}: {quote!r}") from exc
    if displayed != value:
        raise QuotePrecisionError(f"Quote {quote!r} exceeds declared precision {precision}")
    text = f"{displayed:.{precision}f}"
    if precision == 0:
        return text, None
    return text, int(text[-1])

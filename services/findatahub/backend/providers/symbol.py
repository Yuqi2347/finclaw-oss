def normalize_a_share_ticker(ticker: str) -> str:
    value = ticker.strip().upper()
    if len(value) == 8 and value[:2] in {"SH", "SZ", "BJ"} and value[2:].isdigit():
        return f"{value[2:]}.{value[:2]}"
    if value.endswith(".SH") or value.endswith(".SZ") or value.endswith(".BJ"):
        return value
    if value.startswith(("5", "6", "9")):
        return f"{value}.SH"
    if value.startswith(("0", "1", "2", "3")):
        return f"{value}.SZ"
    if value.startswith(("4", "8")):
        return f"{value}.BJ"
    return value


def to_akshare_symbol(ticker: str) -> str:
    return normalize_a_share_ticker(ticker).split(".", 1)[0]


def to_sina_symbol(ticker: str) -> str:
    normalized = normalize_a_share_ticker(ticker)
    code = to_akshare_symbol(normalized)
    if normalized.endswith(".SH"):
        return f"sh{code}"
    if normalized.endswith(".SZ"):
        return f"sz{code}"
    if normalized.endswith(".BJ"):
        return f"bj{code}"
    raise ValueError(f"Unsupported A-share ticker for Sina: {ticker}")


def infer_exchange(ticker: str) -> str | None:
    value = normalize_a_share_ticker(ticker)
    if value.endswith(".SH"):
        return "SSE"
    if value.endswith(".SZ"):
        return "SZSE"
    if value.endswith(".BJ"):
        return "BSE"
    return None

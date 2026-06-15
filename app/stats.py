_stats: dict = {"processed": 0, "fallback": 0, "errors": 0}


def get() -> dict:
    return dict(_stats)


def increment(key: str) -> None:
    _stats[key] = _stats.get(key, 0) + 1

"""Only attributed, reviewed peer data may be called an industry benchmark."""


def sourced_benchmark_value(spec: dict, industry_entry: dict):
    if not isinstance(spec, dict):
        return None
    source = spec.get('source_metadata') or industry_entry.get('source_metadata') or {}
    if not isinstance(source, dict) or source.get('verified') is not True:
        return None
    if not all(source.get(key) for key in ('source', 'period', 'sample', 'scope')):
        return None
    if source.get('comparability_reviewed') is not True:
        return None
    value = spec.get('average')
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

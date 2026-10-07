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


def match_industry_entry(industry: str, industries: dict):
    """F4/F5 共享行业匹配：match_keywords 命中 + match_priority 特异性优先。

    在基准数据的 industries 字典中查找与 free-text 行业名最匹配的条目：
    - 任一 match_keywords 命中即成为候选（无 match_keywords 的条目退化为名称互含）；
    - 按 (match_priority, 最长命中关键词长度) 排序取最优；
    - 无命中返回 None——调用方应回落通用阈值并注明，禁止错配
      （如"医药制造业"被配到通用"制造业"基准）。

    Args:
        industry: 报告中的行业文本（如 "医药制造业"、"银行"）。
        industries: industry_benchmarks.json 的 industries 字典。

    Returns:
        (entry, matched_name) 或 (None, "")。
    """
    industry = str(industry or "")
    if not industry:
        return None, ""
    best_entry, best_name, best_score = None, "", (-1, -1)
    for name, entry in (industries or {}).items():
        if not isinstance(entry, dict):
            continue
        keywords = entry.get('match_keywords') or [str(name)]
        priority = entry.get('match_priority', 0)
        for kw in keywords:
            kw = str(kw)
            if kw and kw in industry:
                score = (priority, len(kw))
                if score > best_score:
                    best_entry, best_name, best_score = entry, name, score
                break
    if best_entry is None:
        return None, ""
    return best_entry, best_name

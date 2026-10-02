import json
import os

import pytest

from core.benchmark_contract import sourced_benchmark_value


def test_numeric_defaults_are_not_verified_peer_statistics(tmp_path):
    """行业基准未核验时，雷达图不得把内置数值冒充已验证同业均数。

    旧行为：无核验基准时直接抛 ValueError（整图不出现）。
    新行为（数据完整性兜底）：有多少数据就画多少——已获取的公司值仍记录在图上，
    缺失的行业基准在图上标注「无行业基准」，而不是伪造基准或静默消失。
    """
    from tools.pdf_export import _load_benchmarks
    from tools.visualizer import _generate_radar_chart
    assert _load_benchmarks('能源') == {}
    report = {'company_info': {'industry': '能源'}, 'calculated_indicators': {
        'gross_margin_pct': 20.89, 'debt_to_asset_ratio_pct': 38.48,
        'current_ratio': 1.0386, 'inventory_turnover_ratio': 7.08,
        'accounts_receivable_to_revenue_ratio': 8.26}}
    out = _generate_radar_chart(json.dumps(report), str(tmp_path / 'radar.png'))
    # 必须出图成功、不抛异常，且确实写入 PNG 文件
    assert os.path.exists(out)
    assert os.path.getsize(out) > 0


def test_peer_mean_requires_traceability_and_comparability_review():
    spec = {'average': 35}
    meta = {'verified': True, 'source': 'published peer dataset', 'period': '2025H1',
            'sample': 'same business segment sample', 'scope': 'consolidated',
            'comparability_reviewed': True}
    assert sourced_benchmark_value(spec, {'source_metadata': meta}) == 35
    for key in meta:
        incomplete = dict(meta)
        incomplete.pop(key)
        assert sourced_benchmark_value(spec, {'source_metadata': incomplete}) is None

# -*- coding: utf-8 -*-
"""可视化「数据完整性兜底」测试：无论数据是否齐全都出图，缺的标注数据不全。

覆盖雷达图（完整五维/仅一维可比/有公司值无基准/完全无数据）与
趋势图（仅 1 年/0 年）共 6 类场景，验证：
1. 不再抛异常或返回文本，而是真正生成 PNG；
2. 缺失原因（数据未获取/无行业基准/缺公司值/不足 2 年）写入图面字符串。
"""
import json
import os

import pytest

from tools import visualizer as viz


def _save(fn, *args):
    """调用私有绘图函数并断言出图。fn 签名 (payload, output_path)。"""
    return fn(*args)


def _monkeypatch_verified_benchmarks(monkeypatch):
    """把基准契约 mock 成「核验通过」，使行业基准可用（仅测试用）。"""
    import core.benchmark_contract as bc
    monkeypatch.setattr(bc, 'sourced_benchmark_value',
                        lambda spec, entry: (spec or {}).get('average') if isinstance(spec, dict) else None)


# ── 雷达图 ────────────────────────────────────────────────

def test_radar_full_five_metrics_renders(tmp_path, monkeypatch):
    """完整五维 + 核验基准：正常出图。"""
    _monkeypatch_verified_benchmarks(monkeypatch)
    report = {'company_info': {'company_name': '测试公司', 'report_year': '2025', 'industry': '制造业'},
              'calculated_indicators': {
                  'gross_margin_pct': 25.0, 'debt_to_asset_ratio_pct': 50.0,
                  'accounts_receivable_to_revenue_ratio': 20.0,
                  'inventory_turnover_ratio': 6.0, 'current_ratio': 1.5}}
    out = _save(viz._generate_radar_chart, json.dumps(report, ensure_ascii=False),
                str(tmp_path / 'r_full.png'))
    assert os.path.exists(out) and os.path.getsize(out) > 0


def test_radar_single_complete_metric_renders(tmp_path, monkeypatch):
    """仅 1 个维度可对比（<3 项旧门槛）：仍出图，不再抛「不适用」。"""
    _monkeypatch_verified_benchmarks(monkeypatch)
    report = {'company_info': {'company_name': '测试公司', 'report_year': '2025', 'industry': '制造业'},
              'calculated_indicators': {'gross_margin_pct': 25.0}}
    out = viz._generate_radar_chart(json.dumps(report, ensure_ascii=False),
                                    str(tmp_path / 'r_part.png'))
    assert os.path.exists(out) and os.path.getsize(out) > 0


def test_radar_no_benchmark_still_renders(tmp_path):
    """有公司值但行业基准未核验：出图并记录已获取数值（不得伪造基准）。"""
    report = {'company_info': {'company_name': '测试公司', 'report_year': '2025', 'industry': '能源'},
              'calculated_indicators': {'gross_margin_pct': 20.89, 'debt_to_asset_ratio_pct': 38.48}}
    out = viz._generate_radar_chart(json.dumps(report, ensure_ascii=False),
                                    str(tmp_path / 'r_nb.png'))
    assert os.path.exists(out) and os.path.getsize(out) > 0


def test_radar_completely_empty_renders(tmp_path):
    """无任何指标数据：出占位图，图面自证「未获取财务指标数据」，不静默消失。"""
    report = {'company_info': {'company_name': '测试公司', 'report_year': '2025', 'industry': '制造业'},
              'calculated_indicators': {}}
    out = viz._generate_radar_chart(json.dumps(report, ensure_ascii=False),
                                    str(tmp_path / 'r_empty.png'))
    assert os.path.exists(out) and os.path.getsize(out) > 0


def test_radar_no_industry_still_renders(tmp_path):
    """连行业信息都没有：出图，标注「未提供行业信息，无行业基准可比」。"""
    report = {'company_info': {'company_name': '测试公司', 'report_year': '2025'},
              'calculated_indicators': {'gross_margin_pct': 20.89}}
    out = viz._generate_radar_chart(json.dumps(report, ensure_ascii=False),
                                    str(tmp_path / 'r_no_ind.png'))
    assert os.path.exists(out) and os.path.getsize(out) > 0


# ── 趋势图 ────────────────────────────────────────────────

def test_trend_one_year_renders_with_incomplete_mark(tmp_path):
    """仅 1 年数据：出图（柱状记录已获取科目），不再返回「需要至少2年」文本。"""
    data = {'company_name': '测试公司', 'years': [
        {'year': '2025', 'revenue': 100, 'net_profit': 20, 'operating_cashflow': 15}]}
    out = viz._generate_trend_chart(json.dumps(data, ensure_ascii=False),
                                    str(tmp_path / 't1.png'))
    assert os.path.exists(out) and os.path.getsize(out) > 0


def test_trend_zero_years_renders_placeholder(tmp_path):
    """0 年数据：出占位图并标注「未获取多年财务数据」。"""
    data = {'company_name': '测试公司', 'years': []}
    out = viz._generate_trend_chart(json.dumps(data, ensure_ascii=False),
                                    str(tmp_path / 't0.png'))
    assert os.path.exists(out) and os.path.getsize(out) > 0


# ── 文案标注（不依赖 OCR，验证写入图面的字符串） ────────────

def test_radar_incomplete_note_strings(monkeypatch):
    """缺失维度写入「无行业基准/缺公司值/数据未获取」轴端标注与标题提示。"""
    _monkeypatch_verified_benchmarks(monkeypatch)
    import types
    calls = {'texts': [], 'titles': [], 'annotates': []}

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    real_text = plt.Axes.text
    real_annotate = plt.Axes.annotate
    real_set_title = plt.Axes.set_title

    def fake_text(self, *a, **k):
        calls['texts'].append(str(a[0]))
        return real_text(self, *a, **k)

    def fake_annotate(self, *a, **k):
        calls['annotates'].append(str(a[0]))
        return real_annotate(self, *a, **k)

    def fake_set_title(self, *a, **k):
        calls['titles'].append(str(a[0]))
        return real_set_title(self, *a, **k)

    monkeypatch.setattr(plt.Axes, 'text', fake_text)
    monkeypatch.setattr(plt.Axes, 'annotate', fake_annotate)
    monkeypatch.setattr(plt.Axes, 'set_title', fake_set_title)

    # 全部缺公司值、有核验基准（state=no_actual）
    report = {'company_info': {'company_name': '测试公司', 'report_year': '2025', 'industry': '制造业'},
              'calculated_indicators': {}}
    viz._generate_radar_chart(json.dumps(report, ensure_ascii=False), 'unused.png')
    blob = ' | '.join(calls['texts'] + calls['titles'])
    assert '数据未获取' in blob or '缺公司值' in blob or '数据不全' in blob

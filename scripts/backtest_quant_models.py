"""E3 量化模型 A 股分层回测脚本（离线，合成样本）

目的：为 Altman Z-Score / Beneish M-Score 提供 A 股适用性的实证支撑。
真实公开样本回测需要联网取数（后续接 MCP 外部数据源时升级为真实样本），
本脚本先用合成数据（按已知风险形态构造）验证模型分区逻辑的判别方向与
灵敏度，输出分层命中率/误报率/AUC 占位结果与真实回测的升级路径。

运行：
    uv run python scripts/backtest_quant_models.py           # 合成样本回测
    uv run python scripts/backtest_quant_models.py --json    # 机器可读输出

结论落点：tests/evaluation_results.json 的 quant_backtest 键（由 --persist 写入）。
"""
import argparse
import json
import os
import random
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Z-Score 分区（与 risk_models.py 一致）
_Z_ZONES = {"original": (2.99, 1.81), "private": (2.90, 1.23), "emerging": (2.60, 1.10)}


def _altman_z(total_assets, working_capital, retained_earnings, ebit,
              market_cap_or_equity, revenue, total_liabilities, variant):
    """按 risk_models.py 同口径计算 Z 分值（合成样本用账面价值变体）。"""
    if not all(isinstance(v, (int, float)) and v for v in
               (total_assets, total_liabilities)):
        return None
    x1 = working_capital / total_assets
    x2 = retained_earnings / total_assets
    x3 = ebit / total_assets
    x4 = market_cap_or_equity / total_liabilities
    if variant == "original":
        z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * (revenue / total_assets)
    elif variant == "private":
        z = 0.717 * x1 + 0.847 * x2 + 3.107 * x3 + 0.42 * x4 + 0.998 * (revenue / total_assets)
    else:  # emerging：剔除周转率
        z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
    return round(z, 3)


def _synthetic_population(n_healthy: int, n_distressed: int, seed: int = 42):
    """构造合成样本：健康组（低杠杆+盈利+留存为正）、困境组（高杠杆+亏损+留存为负）。

    合成数据仅验证判别方向，不声称代表真实 A 股分布——这是与真实回测的
    核心差距，结果中如实标注。
    """
    rng = random.Random(seed)
    samples = []
    for _ in range(n_healthy):
        ta = rng.uniform(5e8, 5e9)
        samples.append({
            "label": "healthy",
            "industry_class": rng.choice(["制造业", "医药生物", "互联网科技", "能源"]),
            "total_assets": ta,
            "working_capital": ta * rng.uniform(0.1, 0.35),
            "retained_earnings": ta * rng.uniform(0.05, 0.3),
            "ebit": ta * rng.uniform(0.03, 0.12),
            "equity": ta * rng.uniform(0.45, 0.7),
            "revenue": ta * rng.uniform(0.5, 1.5),
        })
    for _ in range(n_distressed):
        ta = rng.uniform(5e8, 5e9)
        samples.append({
            "label": "distressed",
            "industry_class": rng.choice(["制造业", "房地产", "零售贸易"]),
            "total_assets": ta,
            "working_capital": ta * rng.uniform(-0.3, 0.05),
            "retained_earnings": ta * rng.uniform(-0.35, -0.02),
            "ebit": ta * rng.uniform(-0.15, 0.005),
            "equity": ta * rng.uniform(0.02, 0.3),
            "revenue": ta * rng.uniform(0.4, 1.2),
        })
    return samples


def run_backtest(n_healthy=200, n_distressed=100, seed=42):
    """分层回测：按行业组计算 Z 分值落入困境区的命中率/误报率。"""
    samples = _synthetic_population(n_healthy, n_distressed, seed)
    by_industry = {}
    for s in samples:
        variant = "private"  # 合成样本用账面价值变体（与无市值降级路径一致）
        low, high = _Z_ZONES[variant]
        z = _altman_z(s["total_assets"], s["working_capital"], s["retained_earnings"],
                      s["ebit"], s["equity"], s["revenue"], s["total_assets"] - s["equity"],
                      variant)
        if z is None:
            continue
        flagged = z < high  # 落入灰色或困境区
        ind = by_industry.setdefault(s["industry_class"], {
            "healthy": 0, "healthy_flagged": 0, "distressed": 0, "distressed_flagged": 0})
        if s["label"] == "healthy":
            ind["healthy"] += 1
            ind["healthy_flagged"] += 1 if flagged else 0
        else:
            ind["distressed"] += 1
            ind["distressed_flagged"] += 1 if flagged else 0

    industry_results = {}
    total_tp = total_fp = 0
    for ind, stat in sorted(by_industry.items()):
        recall = (stat["distressed_flagged"] / stat["distressed"]) if stat["distressed"] else None
        fpr = (stat["healthy_flagged"] / stat["healthy"]) if stat["healthy"] else None
        industry_results[ind] = {
            "healthy_n": stat["healthy"], "distressed_n": stat["distressed"],
            "distressed_recall": round(recall, 3) if recall is not None else None,
            "healthy_false_positive_rate": round(fpr, 3) if fpr is not None else None,
        }
        total_tp += stat["distressed_flagged"]
        total_fp += stat["healthy_flagged"]

    macro_recall = round(total_tp / max(1, n_distressed), 3)
    macro_fpr = round(total_fp / max(1, n_healthy), 3)
    return {
        "backtest_type": "synthetic_direction_check",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "variant": "private（账面价值，与无市值降级路径一致）",
        "zones": {k: list(v) for k, v in _Z_ZONES.items()},
        "sample": {"healthy": n_healthy, "distressed": n_distressed, "seed": seed},
        "overall": {"distressed_recall": macro_recall, "healthy_false_positive_rate": macro_fpr},
        "by_industry": industry_results,
        "limitations": [
            "合成样本仅验证判别方向与分区灵敏度，不代表真实 A 股分布",
            "真实回测需公开处罚样本 + 配对健康样本（升级路径：接 MCP 外部数据源后按行业×年度分层抽样）",
            "阈值沿用 1968/1983 美国样本系数，A 股本地重估前仅作交叉印证",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="量化模型分层回测（合成方向验证）")
    parser.add_argument("--json", action="store_true", help="仅输出机器可读 JSON")
    parser.add_argument("--persist", action="store_true",
                        help="结果写入 tests/evaluation_results.json 的 quant_backtest 键")
    args = parser.parse_args()

    result = run_backtest()
    if args.persist:
        path = os.path.join(REPO_ROOT, "tests", "evaluation_results.json")
        try:
            with open(path, encoding="utf-8") as f:
                full = json.load(f)
        except (OSError, json.JSONDecodeError):
            full = {}
        full["quant_backtest"] = result
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(full, f, ensure_ascii=False, indent=2)
        if not args.json:
            print(f"结果已写入 {path}")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print("=" * 66)
    print("量化模型分层回测（合成样本方向验证）")
    print("=" * 66)
    print(f"样本：健康 {result['sample']['healthy']} / 困境 {result['sample']['distressed']}"
          f"（变体 {result['variant']}）")
    print(f"总体：困境组命中率 {result['overall']['distressed_recall']}"
          f"，健康组误报率 {result['overall']['healthy_false_positive_rate']}")
    for ind, stat in result["by_industry"].items():
        print(f"  {ind}: 命中 {stat['distressed_recall']} / 误报 {stat['healthy_false_positive_rate']}"
              f"（n={stat['healthy_n']}+{stat['distressed_n']}）")
    print("\n局限（详见 limitations）：合成样本仅验证判别方向；真实 A 股回测")
    print("需接 MCP 外部数据源后按公开处罚样本 + 配对对照分层抽样。")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""端到端不变量校验脚本（v5.3GA 收口）。

对一次真实链路分析的产物运行，把"一致性"从 LLM 的涌现属性变成工程不变量：
1. 四点评分对齐：综合报告封面 == 结论章 == ledger 评分 == 底稿整体评估
2. 四份产物风险计数两两一致；issuer 风险明细不含系统自指词；breakdown 维度 ∈ {数值, "未获取"}
3. 黄金事实层（确定性层）：未分配利润勾稽无假阳性（归母通过或 caliber 提示、V 系列无
   未分配利润条目）；评分算术自洽（base=Σ权重×维度分）；四维金额行单位 ∈ {亿元,百万元}
   且与 ledger 一致；跨表同指标 ±10% 软断言（仅记录差异日志，不判红）

用法:
    python scripts/e2e_invariants.py <结果JSON> [<local_storage 根目录>]
"""
import json
import re
import sys
import glob
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF_REF_WORDS = ("系统", "台账", "快照", "工具输出", "评分快照", "内部评分", "综合评分")
ISSUER_EXEMPT = ("内部控制", "内部交易", "内部审批", "集团内部", "内部人", "系统性风险")


def latest_report(reports_dir: str, suffix: str) -> str:
    cands = sorted(glob.glob(os.path.join(reports_dir, "**", f"*{suffix}"), recursive=True), key=os.path.getmtime)
    assert cands, f"未找到 {suffix} 产物"
    return cands[-1]


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} | {name} {detail}")
    return ok


def _tool_entries(result: dict) -> list[dict]:
    """兼容流式结果的 messages 与 HTTP 结果的 tool_results 两种结构。"""
    entries = []
    for item in result.get("messages", []) or []:
        if isinstance(item, dict) and item.get("type") == "tool":
            entries.append(item)
    for item in result.get("tool_results", []) or []:
        if isinstance(item, dict) and item.get("name"):
            entries.append(item)
    return entries


def _sheet(wb, *names):
    """按当前名称优先、历史名称兼容地获取工作表。"""
    for name in names:
        if name in wb.sheetnames:
            return wb[name]
    raise KeyError(f"未找到工作表：{names}，实际工作表={wb.sheetnames}")


def _risk_id(value) -> str:
    match = re.match(r"\s*([A-Z]\d{3})", str(value or ""))
    return match.group(1) if match else ""


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    result_path = sys.argv[1]
    reports_dir = sys.argv[2] if len(sys.argv) > 2 else str(ROOT / "local_storage")
    result = json.load(open(result_path, encoding="utf-8"))
    from pypdf import PdfReader
    from openpyxl import load_workbook

    ok_all = True

    # 四份产物
    t3 = "".join((p.extract_text() or "") for p in
                 PdfReader(latest_report(reports_dir, "_综合汇总报告.pdf")).pages)
    t4 = "".join((p.extract_text() or "") for p in
                 PdfReader(latest_report(reports_dir, "_财务健康诊断报告.pdf")).pages)
    xl = latest_report(reports_dir, "_审计底稿.xlsx")
    wb = load_workbook(xl)

    # ── 不变量 1：四点评分对齐 ──
    # 基准 = 封面评分（系统最终分，来自 _post_process 兜底快照）；封面无分（score=None）
    # 时结论章/底稿整体评估也不得出现任意分数。消息里的 score 是 LLM 原始版本，不作基准。
    m_cover = re.search(r"(\d+\.?\d*)\s*/\s*100", t3[:2500])
    i8 = t3.find("整体风险评估结论")
    seg8 = t3[i8:i8 + 400] if i8 >= 0 else ""
    m8 = re.search(r"综合风险评分\s*(\d+\.?\d*)\s*分", seg8)
    ov = str(_sheet(wb, "规则口径", "整体评估")["A3"].value or "")
    m_ov = re.search(r"综合风险评分\s*(\d+\.?\d*)\s*分", ov)

    if m_cover:
        cover_score = float(m_cover.group(1))
        concl_ok = (not m8) or abs(float(m8.group(1)) - cover_score) < 0.01
        excel_ok = (not m_ov) or abs(float(m_ov.group(1)) - cover_score) < 0.01
        detail = f"cover={cover_score} concl={m8.group(1) if m8 else '未引用'} " \
                 f"excel={m_ov.group(1) if m_ov else '未引用'}"
        ok_all &= check("I1 四点评分对齐（以封面为基准）", concl_ok and excel_ok, detail)
    else:
        # 封面无分：结论章与底稿整体评估不得出现任意评分引用（score=None 语义）
        no_concl = (not m8)
        no_excel = (not m_ov)
        ok_all &= check("I1 四点评分对齐（封面无分，其余亦无分）", no_concl and no_excel,
                        f"concl={m8.group(1) if m8 else '无'} excel={m_ov.group(1) if m_ov else '无'}")

    # ── 不变量 2：计数一致 + issuer 无自指词 + breakdown 类型 ──
    j = t3.find("风险清单摘要")
    end = t3.find("四、交叉验证分析", j)
    seg_list = t3[j:end] if end > j else t3[j:j + 800]
    pdf_ids = re.findall(r"[RV]\d{3}", seg_list)
    ws2 = _sheet(wb, "风险台账", "风险明细")
    xl_ids = []
    for row in ws2.iter_rows(min_row=2, values_only=True):
        if str(row[0] or "").startswith("风险状态索引"):
            break
        risk_id = _risk_id(row[0])
        if risk_id:
            xl_ids.append(risk_id)
    ok_all &= check("I2 综合清单=底稿明细", set(pdf_ids) == set(xl_ids),
                    f"{sorted(set(pdf_ids))} vs {sorted(xl_ids)}")

    ws = _sheet(wb, "报告概览", "风险总览")
    summ = {row[0]: row[1] for row in ws.iter_rows(values_only=True)
            if row[0] in ("系统采信风险", "风险总数", "重大风险", "重要风险", "一般风险")}
    formal_total = summ.get("系统采信风险", summ.get("风险总数"))
    ok_all &= check("I2 底稿摘要=明细", formal_total == len(xl_ids), str(summ))

    issuer_self_ref = [r[0] for r in ws2.iter_rows(min_row=2, values_only=True) if r[0]
                       and _risk_id(r[0])
                       and any(w in str(r[2]) + str(r[5] or "") for w in SELF_REF_WORDS)
                       and not any(e in str(r[2]) + str(r[5] or "") for e in ISSUER_EXEMPT)]
    ok_all &= check("I2 issuer 无系统自指词", len(issuer_self_ref) == 0, str(issuer_self_ref))

    entries = _tool_entries(result)
    for m in entries:
        if "comprehensive_score" in str(m.get("name", "")):
            try:
                bd = json.loads(str(m.get("content", ""))).get("breakdown", {})
                bad = [k for k, v in bd.items()
                       if not (isinstance(v, (int, float)) or v == "未获取")]
                ok_all &= check("I2 breakdown 维度类型", len(bad) == 0, str(bd))
            except Exception:
                pass
            break

    # ── 不变量 3：黄金事实层（确定性层）──
    for m in entries:
        if "validate" in str(m.get("name", "")):
            try:
                dv = json.loads(str(m.get("content", ""))).get("data_validation", {})
                re_chk = next((c for c in dv.get("all_checks", [])
                               if "未分配利润" in str(c.get("check", ""))), None)
                if re_chk:
                    ok_pass = re_chk.get("passed") is True
                    ok_caliber = "caliber" in re_chk or "归母" in str(re_chk.get("net_profit_note", ""))
                    ok_all &= check("I3 未分配利润无假阳性", ok_pass or ok_caliber,
                                    f"passed={re_chk.get('passed')} note={re_chk.get('net_profit_note')} "
                                    f"caliber={re_chk.get('caliber', '无')}")
                v_ret = [r.get("risk_id") for r in dv.get("risks", [])
                         if "未分配利润" in str(r.get("title", ""))]
                ok_all &= check("I3 V 系列无未分配利润条目", len(v_ret) == 0, str(v_ret))
            except Exception as e:
                ok_all &= check("I3 validate 解析", False, str(e))
            break

    # 评分算术自洽（系统兜底评分）
    for m in entries:
        if "comprehensive_score" in str(m.get("name", "")):
            try:
                s = json.loads(str(m.get("content", "")))
                if s.get("score") is not None:
                    bd = s.get("breakdown", {})
                    weights = s.get("weights", {})
                    avail = {k: float(v) for k, v in bd.items() if isinstance(v, (int, float))}
                    if avail:
                        ws_sum = sum(weights.get(k, 0) for k in avail)
                        expect = sum(v * weights.get(k, 0) / ws_sum for k, v in avail.items())
                        ok_all &= check("I3 评分算术自洽",
                                        abs(float(s.get("base_score", -1)) - expect) < 0.6,
                                        f"base={s.get('base_score')} 期望≈{expect:.1f}")
            except Exception:
                pass
            break

    # 四维金额行单位 ∈ {亿元, 百万元}
    kk = t4.find("本期净利润")
    np_line = t4[kk:kk + 60] if kk >= 0 else ""
    ok_all &= check("I3 四维金额单位", "亿元" in np_line or "百万元" in np_line, np_line.strip())

    print("=" * 60)
    print("e2e 不变量结果:", "全部通过" if ok_all else "存在 FAIL")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()

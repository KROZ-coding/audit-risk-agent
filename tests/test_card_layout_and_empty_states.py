"""卡片布局优先级、未获取原因去重、空热力图可解释性回归。

三个实测缺陷（用户截图提出）：

1. `.report-meta-card` 等卡片声明了 `display:block`，但 `.score-card` 是 flex 且
   定义在样式表更后面，同优先级下后者覆盖前者 —— 元数据标题被压成 21px 宽的竖排
   文字、审查门禁挤成 45px、指标表整块错位。本测试按 CSS 级联规则求实际生效值，
   不依赖样式书写顺序。
2. 数据来源表的「状态」列已写「未获取」，原因列又重复「未获取：」前缀。
3. 台账无正式风险时热力图是一张全白网格，与渲染失败无法区分。
"""
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "..", "src", "web", "index.html")


def _css_text():
    html = io.open(INDEX_HTML, encoding="utf-8").read()
    blocks = re.findall(r"<style>(.*?)</style>", html, re.S)
    assert blocks, "index.html 未找到 <style>"
    return "\n".join(blocks)


def _effective_display(css, classes):
    """按 CSS 级联求元素（class 集合为 classes）实际生效的 display 值。

    只考虑单复合选择器（不含空格/子选择器/伪类），即真正作用于该元素本身的规则；
    按 (特异性=class 个数, 出现顺序) 取最大者，与浏览器「同优先级后者胜出」一致。
    """
    wanted = set(classes)
    best = None
    # 先去注释：选择器捕获会带上前一条规则的注释块，带注释的选择器无法精确匹配
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for order, (selector_list, body) in enumerate(re.findall(r"([^{}]+)\{([^{}]*)\}", css)):
        m = re.search(r"(?:^|;)\s*display\s*:\s*([^;]+)", body)
        if not m:
            continue
        value = m.group(1).strip()
        for selector in selector_list.split(","):
            sel = selector.strip()
            if not re.fullmatch(r"(?:\.[A-Za-z0-9_-]+)+", sel or ""):
                continue
            sel_classes = set(re.findall(r"\.([A-Za-z0-9_-]+)", sel))
            if not sel_classes.issubset(wanted):
                continue
            key = (len(sel_classes), order)
            if best is None or key > best[:2]:
                best = (key[0], key[1], value)
    return best[2] if best else None


class TestCardLayoutCascade:
    """卡片必须纵向堆叠，不能被 .score-card 的 flex 覆盖。"""

    def test_stacking_cards_resolve_to_block(self):
        css = _css_text()
        for cls in ("report-meta-card", "review-gate-card", "metric-view-card",
                    "artifact-manifest-card"):
            got = _effective_display(css, {"score-card", cls})
            assert got == "block", f".score-card.{cls} 实际生效 display={got!r}，应为 block"

    def test_score_card_itself_still_flex(self):
        """对照：普通 score-card 仍按 flex 横排，未被上面的修复误伤。"""
        assert _effective_display(_css_text(), {"score-card"}) == "flex"


class TestMissingSourceReasonNotDuplicated:
    """未获取原因不得重复「未获取」前缀（状态列已表达）。"""

    def test_web_notes_are_reason_only(self):

        from main import _build_data_sources

        sources = _build_data_sources({})
        notes = [s["note"] for s in sources if not s["used"]]
        assert len(notes) == 7, "无工具结果时 7 项数据源应全部未获取"
        for note in notes:
            assert note and not note.startswith("未获取"), f"原因重复状态列前缀: {note}"

    def test_used_source_has_empty_note(self):

        from main import _build_data_sources

        idx = {"calculate_financial_indicators": json.dumps({"metrics": []}, ensure_ascii=False)}
        sources = {s["name"]: s for s in _build_data_sources(idx)}
        assert sources["财务指标数据"]["used"] is True
        assert sources["财务指标数据"]["note"] == ""

    def test_audit_opinion_from_disclosure_counts_as_used(self):
        """审计意见双源判定：识别工具未调用，但披露检查给了意见即算已获取。"""

        from main import _build_data_sources

        idx = {"check_disclosure_compliance": json.dumps({"audit_opinion": "未经审计（半年度报告）"},
                                                        ensure_ascii=False)}
        sources = {s["name"]: s for s in _build_data_sources(idx)}
        assert sources["审计意见识别"]["used"] is True
        assert sources["审计意见识别"]["note"] == ""

    def test_source_notes_have_no_prefix_in_web_and_pdf(self):
        """源码级守护：网页与 PDF 两处原因文案都不再带前缀。"""
        for rel in ("../src/main.py", "../src/tools/pdf_export.py"):
            text = io.open(os.path.join(HERE, rel), encoding="utf-8").read()
            assert '"未获取：' not in text, f"{rel} 仍存在「未获取：」前缀文案"


class TestEmptyHeatmapIsExplainable:
    """空热力图必须能自证「没有数据」，而不是一张全白网格。"""

    def test_reason_when_no_candidates(self):
        from tools.visualizer import _empty_heatmap_reason
        assert _empty_heatmap_reason({"risk_details": []}, []) == "台账未包含风险条目"

    def test_reason_when_candidates_not_accepted(self):
        from tools.visualizer import _empty_heatmap_reason
        data = {"risk_details": [{"risk_id": "R001", "dimension": "财务错报风险", "level": "重要"}]}
        assert _empty_heatmap_reason(data, []) == "候选风险均未通过证据门禁，不计入正式风险"

    def test_reason_when_formal_risks_out_of_scope(self):
        from tools.visualizer import _empty_heatmap_reason
        data = {"risk_details": [{"risk_id": "R001"}]}
        assert _empty_heatmap_reason(data, [{"risk_id": "R001"}]) == "正式风险的维度或等级不在本图口径内"

    def test_reason_distinguishes_three_sources(self):
        from tools.visualizer import _empty_heatmap_reason
        reasons = {
            _empty_heatmap_reason({"risk_details": []}, []),
            _empty_heatmap_reason({"risk_details": [{"risk_id": "R001"}]}, []),
            _empty_heatmap_reason({"risk_details": [{"risk_id": "R001"}]}, [{"risk_id": "R001"}]),
        }
        assert len(reasons) == 3, "三种空图来源必须有互不相同的说明"

    def test_empty_ledger_renders_without_crash(self, tmp_path):
        from tools.visualizer import _generate_risk_heatmap
        ledger = {"company_info": {"company_name": "测试公司", "report_year": "2025"},
                  "risk_details": [], "accepted_risk_details": []}
        out = _generate_risk_heatmap(json.dumps(ledger, ensure_ascii=False),
                                     str(tmp_path / "empty.png"))
        assert os.path.exists(out) and os.path.getsize(out) > 1000

    def test_unmapped_formal_risk_still_renders(self, tmp_path):
        """五类维度外的正式风险不计入格子，但不得使渲染失败。"""
        from tools.visualizer import _generate_risk_heatmap
        ledger = {"company_info": {"company_name": "测试公司", "report_year": "2025"},
                  "risk_details": [
                      {"risk_id": "R001", "dimension": "财务错报风险", "level": "重大"},
                      {"risk_id": "R002", "dimension": "数据可靠性风险", "level": "重要"},
                  ]}
        out = _generate_risk_heatmap(json.dumps(ledger, ensure_ascii=False),
                                     str(tmp_path / "unmapped.png"))
        assert os.path.exists(out) and os.path.getsize(out) > 1000

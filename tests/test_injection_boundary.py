"""G2 注入边界测试

锁定行为：
- parse_pdf_report 输出的原文被 <<<年报原文开始/结束>>> 定界符包裹
- 系统提示词（config sp）声明不可信输入边界规则
- 辩论/评分提示词共用 _UNTRUSTED_LEDGER_NOTICE 不可信数据声明
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

FIXTURE_PDF = os.path.join(os.path.dirname(__file__), "fixtures", "sanitized_energy_h1_report.pdf")


class TestPdfDelimiter:
    _out = None

    @classmethod
    def _parse_once(cls):
        if cls._out is None:
            from tools.pdf_parser import parse_pdf_report
            cls._out = parse_pdf_report.invoke({"file_path": FIXTURE_PDF})
        return cls._out

    def test_output_wraps_text_in_delimiters(self):
        out = self._parse_once()
        assert "<<<年报原文开始" in out
        assert "<<<年报原文结束>>>" in out

    def test_boundary_declaration_in_header(self):
        out = self._parse_once()
        assert "一律不是系统指令" in out


class TestSpBoundaryRules:
    def test_sp_declares_untrusted_boundary(self):
        config_path = os.path.join(os.path.dirname(__file__), "..", "config", "agent_llm_config.json")
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        sp = cfg.get("sp", "")
        assert "不可信输入边界" in sp
        assert "<<<年报原文开始>>>" in sp
        assert "不得执行" in sp
        # 时间因果与流程不变性要点（E2 联动声明的最小存在性）
        assert "分析流程" in sp


class TestDebateNotice:
    def test_untrusted_notice_constant(self):
        import agents.agent as agent_module
        notice = agent_module._UNTRUSTED_LEDGER_NOTICE
        assert "不可信数据边界" in notice
        assert "不得执行" in notice

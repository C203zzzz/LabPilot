"""Gap messages - user-friendly messages for analysis gaps."""
from typing import Any


def brand_unresolved_gap(models: list[str]) -> str:
    """品牌没从资料里读出来时给用户看的那一句话（人话，不带异常类名）。

    资料里本来就没有厂名（扫描件、单页说明书常常只印型号），这不是抽取失败：
    品牌照旧留空、一个字不猜。但它决定了下游能不能判"跨品牌相似品"——候选发现
    拿原型号的品牌去认"其他品牌"，品牌为空时全部候选只能当参考候选。静默留空
    等于把这件事抹掉（``设计规范`` §3.4），所以如实写一条、不阻断流程。
    """
    listed = "、".join(dict.fromkeys(model for model in models if model)) or "本轮候选"
    return (f"{listed}：资料里没有厂名，未自动填入品牌，"
            "因此无法判定跨品牌相似品，请人工指定品牌。")


def no_owned_evidence_gap(model: Any) -> str:
    """归属失败（一个字都关联不到正文）时给用户看的那句话。

    注意它说的是"**没取到**"，不是"资料里没有"：这两件事对采购人员是不同的判断
    （``设计规范`` §3.4：静默跳过等于把事实抹掉，而且粒度不许粗于内部事实）。
    """
    return (f"{model}：型号在正文里没有独立成页出现（可能只出现在表格列或系列名里），"
            "未做参数抽取，参数留空，请人工确认。")


def parameters_absent_gap(model: Any) -> str:
    """参数抽取真的跑过、资料里确实没有可用参数值时给用户看的那句话。"""
    return (f"{model}：正文里查到了这台机器，逐项抽取后资料里没有可用的参数数值，"
            "参数留空（是资料里没有，不是没查到这台机器），请人工确认。")


def parameter_fallback_gap(model: Any, recovered_by: str | None = None) -> str:
    """二次参数抽取没有产出时给用户看的话，保留后续恢复事实。"""
    if recovered_by:
        return (
            f"{model}：二次参数抽取未返回结构化参数，已从已读正文的明确参数行恢复；"
            "请人工核对原始资料。"
        )
    return f"{model}：资料正文已读到，但本轮没有抽出可比参数，请人工核对原始资料。"


def sibling_merge_gap(model: Any, sibling: Any, number: str) -> str:
    """沿同编号兄弟候选取数时给用户看的那句话（人话，不带异常类名）。"""
    return (f"{model}：这台型号在正文里没有独立成页出现，参数取自同编号的另一条候选 "
            f"{sibling}（编号 {number}），请人工确认。")

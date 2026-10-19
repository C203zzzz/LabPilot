"""Short, evidence-based task names without an additional model request."""
import re


INSTRUMENTS = (
    "小动物麻醉呼吸系统", "小动物麻醉机", "小动物呼吸机", "麻醉机", "呼吸机",
    "多功能微孔板检测仪", "多功能酶标仪", "微孔板检测仪", "酶标仪",
    "紫外可见分光光度计", "紫外分光光度计", "分光光度计", "分光光度仪",
    "荧光定量PCR仪", "实时荧光定量PCR仪", "PCR仪", "扩增仪",
    "高速冷冻离心机", "冷冻离心机", "高速离心机", "离心机",
    "恒温振荡培养箱", "生化培养箱", "恒温培养箱", "培养箱", "摇床",
    "高压蒸汽灭菌器", "高压灭菌锅", "灭菌器", "灭菌锅",
    "生物安全柜", "超净工作台", "通风橱", "流式细胞仪",
    "气相色谱质谱联用仪", "液相色谱质谱联用仪", "气相色谱仪", "液相色谱仪",
    "质谱仪", "测序仪", "显微镜", "电子天平", "分析天平",
    "超声波细胞破碎仪", "超声波清洗机", "冻干机", "超低温冰箱",
    "纯水机", "超纯水系统", "移液器", "水浴锅", "干燥箱",
    "核酸提取仪", "核酸纯化仪", "冷藏培养箱", "BOD培养箱",
    "pH计", "酸度计", "恒温水浴", "冷冻干燥机", "总有机碳分析仪", "TOC分析仪", "凝胶成像系统", "细胞计数仪",
)
ALIASES = ((r"microplate\s+reader", "酶标仪"), (r"spectrophotometer", "分光光度计"),
           (r"centrifuge", "离心机"), (r"incubator", "培养箱"), (r"autoclave", "灭菌器"),
           (r"\bpH\s*(?:meter|benchtop)\b|\bbenchtop\s+pH\b", "pH计"),
           (r"water\s+bath", "恒温水浴"), (r"freeze\s+dryer", "冷冻干燥机"),
           (r"\bmicroscope\b", "显微镜"), (r"\bcell\s+counter\b", "细胞计数仪"),
           (r"\bTOC\s+analy[sz]er\b", "总有机碳分析仪"))


def document_subject(document):
    # Cover headings precede application lists, accessories and comparison tables.
    # Use the first heading mentioning an instrument, not keywords from a manual.
    text = str(document.get("extracted_text") or "")[:1200]
    for line in text.splitlines()[:20]:
        names = instrument_names(line)
        if names:
            return names[:1]
    return instrument_names(document.get("source_name"))[:1]


def instrument_names(text):
    text = str(text or "")
    found = []
    for name in sorted(INSTRUMENTS, key=len, reverse=True):
        if name.casefold() in text.casefold():
            found.append(name)
            text = re.sub(re.escape(name), " ", text, flags=re.I)
    for pattern, label in ALIASES:
        if re.search(pattern, text, re.I) and label not in found:
            found.append(label)
    return found


def task_title(task, candidates=(), documents=()):
    active = [item for item in candidates if item.get("status") not in {"excluded", "rejected"}]
    # A task's explicit purchase subject is more reliable than incidental
    # accessories or instruments mentioned in a long manual.
    names = instrument_names(task.get("scenario"))
    if not names:
        names = list(dict.fromkeys(name for doc in documents for name in document_subject(doc)))
    if not names:
        evidence = " ".join(str(source.get("excerpt") or "") for item in active for source in item.get("sources", []))
        names = instrument_names(evidence)
    if not names:
        names = instrument_names(" ".join(str(doc.get("source_name") or "") for doc in documents))
    if names:
        return "、".join(names[:2]) + ("等仪器选型" if len(names) > 2 else "选型")
    models = list(dict.fromkeys(str(item.get("model") or "").strip() for item in active))
    models = [model for model in models if model and len(model) <= 32]
    if models:
        return models[0] + ("等设备选型" if len(models) > 1 else "选型")
    return "实验室仪器选型"


def task_identity(task, candidates=(), documents=(), baseline=None):
    """Display a confirmed category separately from inferred document subjects."""
    baseline = baseline or {}
    category = str(baseline.get('category') or '').strip()
    inferred = instrument_names(task_title(task, candidates, documents))
    return {'category': category or '、'.join(inferred[:2]),
            'category_source': 'baseline' if category else 'inferred' if inferred else 'unknown',
            'brand': baseline.get('brand') or '', 'model': baseline.get('model') or '',
            **({'title': category + '选型'} if category else {})}

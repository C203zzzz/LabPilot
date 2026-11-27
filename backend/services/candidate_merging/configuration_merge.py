"""候选配置文本的合并（按句去重拼接）。

拆分前 deepseek_analysis 里 ``_merge_configuration`` 定义过两次，运行时生效的是后一次
（按 ``；``/``;`` 分段去重）。拆分时两个调用点各抄了一份、还抄到了被覆盖掉的旧版，
导致同一段配置在"兄弟别名合并"和"清单候选合并"两条路径上结果不同。这里只留一份，
内容与运行时生效的那份逐字一致。
"""
from __future__ import annotations

import re
from typing import Any


def merge_configuration(base: Any, extra: Any) -> str:
    """两段配置文本按句去重拼接，只留信息、不覆盖。"""
    parts, seen = [], set()
    for text in (base, extra):
        for part in re.split(r"[；;]", str(text or "")):
            part = part.strip()
            key = re.sub(r"[\s,，。]", "", part)
            if not part or key in seen:
                continue
            seen.add(key)
            parts.append(part)
    return "；".join(parts)

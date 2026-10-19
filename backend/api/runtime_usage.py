"""只读的运行时用量接口：让"额度还剩多少"在上传／开跑之前就看得见。

背景（盲测单实测）：额度耗尽之前用户拿不到任何预兆，只能"跑到一半突然被挡"。
这个接口只读、不改默认上限、不写账本——上限调到多少是成本决策（见本单报告）。
"""
from fastapi import APIRouter

router = APIRouter(prefix='/api/v1/runtime', tags=['runtime'])


@router.get('/model-budget')
def model_budget() -> dict:
    """本小时共享的模型调用额度：已用／上限／剩余／多久后有空名额。

    ``notice`` 非空时是**已经写好的提醒句**（同一句话由后端给，前端照抄显示，
    免得两处各写一句又慢慢走样）。``exhausted_message`` 是被拦住时那句话的原文，
    与三个错误出口逐字相同。
    """
    from backend.infrastructure.deepseek import model_call_budget
    return model_call_budget()

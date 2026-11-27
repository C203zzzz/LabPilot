"""小零易购（``gdegp.gds.edu.cn:8093``）成交明细本地库：**纯规则**解析与建库。

这个模块只做一件事：把结果公示的列表页与详情页，按页面自己印着的表格列关系
读成「一行商品 = 一条成交明细」，落进一个本地 SQLite。**全程正则/规则，不调用
任何模型。**

设计上不复用 ``education_platform_sources`` 里那条"喂给采信判据"的路径来做解析，
而是**独立**地按 HTML 单元格解析同一张表；两条路径的结果在 ``cross_check`` 里
逐单比对（``型号``/``单价``/``数量`` 三元组必须一致），所以"两套彼此独立的解析
给出同一个数"这件事在这个库里是**每次建库都跑一遍**的，不是一次性结论。

三件事必须说清楚，写在代码里也写在文档里：

* **「最高限价」永远不是成交价**。反拍页的成交单价只有「中标价」一列；
  实测同一行限价 28656 / 中标价 15000，取错列会系统性高估。
* **直购单（协议价格 = 0）的成交价就是商品目录挂牌价**，性质与议价折扣不同，
  单独标成 ``price_kind='list_price_direct'``，不与议价单混。
* **个人信息一格都不落盘**。落盘前先跑产品自己的 ``redact_payload``，再跑本模块
  的 ``pii_residue`` 闸门；闸门有命中就**不写这一页**并记一条缺口。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from html import unescape
from pathlib import Path

from backend.services.education_platform_sources import (
    GuangdongXiaolingyigouPlatform, redact_detail)

PLATFORM = GuangdongXiaolingyigouPlatform()
PLATFORM_KEY = 'xlyg'

SCHEMA_PATH = Path(__file__).with_name('xlyg_schema.sql')

#: 探测／抓取用的品类词。词表从 ``设计规范`` §2 的 10 个入选品类出发，再加同义/上位词。
PROBE_KEYWORDS = (
    '离心机', '台式离心机', '高速离心机', '冷冻离心机',
    '纯水机', '超纯水机',
    '磁力搅拌器',
    '电子天平',
    '马弗炉', '箱式电阻炉',
    'pH计', '酸度计',
    '电热鼓风干燥箱', '干燥箱',
    '超声波清洗机',
    '紫外可见分光光度计', '分光光度计',
    '旋转蒸发仪',
    '灭菌器', '灭菌锅',
    '生物安全柜',
    '培养箱',
    '水浴锅',
    '振荡器',
    '匀浆机',
    '色谱',
    '移液器',
    '显微镜',
    '超低温冰箱',
)
#: 第二轮补的词：第一轮实抓 1481 行、离 1,500 差 19 行，补这几个词既补行数也补
#: 类别覆盖。先按 84 个月窗口探过量（见 ``output/xlyg-db/summary_candidates.txt``），
#: 粘度计 0 条、浊度计 3 条且全为终止单，没进词表。
PROBE_KEYWORDS = PROBE_KEYWORDS + (
    '酶标仪', '超净工作台', '通风柜', '冷冻干燥机', '真空泵',
    '水分测定仪', '电导率仪', '消解仪', '熔点仪', '氮吹仪',
)

#: 类别识别词表：**比检索词表宽得多**，两者用途不同，不要合并。
#:
#: * ``PROBE_KEYWORDS`` 是**检索词**：为了"用最少的请求探到最多的单"而挑的 39 个词，
#:   服务于"按项目名去站点搜"这条路径；
#: * 本表是**类别识别词**：全量入库后每一行都要定类别，只靠那 39 个词实测有 14.2%
#:   认不出来（旧库 1,634 行里 232 行），而认不出的全是正经仪器——洁净工作台、
#:   恒温金属浴、医用冷藏箱、PCR 仪、分析天平、电泳仪。所以按"实验室常见品类"补宽。
#:
#: 匹配两条规矩（``assign_category``）：**大小写不敏感**（站点的商品名里 ``PH计``
#: 与 ``pH计`` 都出现过）、**取最长命中**（「分析天平」不会被更短的「天平」抢走）。
CATEGORY_OF = {
    # -- 分离与离心 --------------------------------------------------------
    '离心机': '离心机', '离心器': '离心机', '离心仪': '离心机',
    '台式离心机': '离心机', '高速离心机': '离心机', '冷冻离心机': '离心机',
    '微量离心机': '离心机', '离心浓缩仪': '离心机',
    # -- 称量 --------------------------------------------------------------
    '电子天平': '电子天平', '分析天平': '电子天平', '精密天平': '电子天平',
    '万分之一天平': '电子天平', '十万分之一天平': '电子天平', '微量天平': '电子天平',
    '天平': '电子天平',
    # -- 纯水 --------------------------------------------------------------
    '纯水机': '纯水机/超纯水机', '纯水器': '纯水机/超纯水机', '纯水仪': '纯水机/超纯水机',
    '超纯水机': '纯水机/超纯水机', '超纯水器': '纯水机/超纯水机',
    '纯水系统': '纯水机/超纯水机', '去离子水机': '纯水机/超纯水机',
    # -- 搅拌 --------------------------------------------------------------
    '磁力搅拌器': '磁力搅拌器', '加热搅拌器': '磁力搅拌器', '磁力搅拌': '磁力搅拌器',
    '搅拌器': '磁力搅拌器', '搅拌台': '磁力搅拌器', '搅拌机': '磁力搅拌器',
    # -- 高温炉 ------------------------------------------------------------
    '马弗炉': '马弗炉/箱式电阻炉', '箱式电阻炉': '马弗炉/箱式电阻炉',
    '电阻炉': '马弗炉/箱式电阻炉', '高温炉': '马弗炉/箱式电阻炉',
    '管式炉': '马弗炉/箱式电阻炉', '气氛炉': '马弗炉/箱式电阻炉',
    '真空炉': '马弗炉/箱式电阻炉', '烧结炉': '马弗炉/箱式电阻炉',
    # -- 酸度与电化学 ------------------------------------------------------
    'pH计': 'pH计/酸度计', '酸度计': 'pH计/酸度计', '酸碱度计': 'pH计/酸度计',
    'pH酸度计': 'pH计/酸度计',
    '电导率仪': '电导率仪', '电导仪': '电导率仪', '电导率': '电导率仪',
    # -- 干燥与箱体 --------------------------------------------------------
    '电热鼓风干燥箱': '干燥箱/烘箱', '鼓风干燥箱': '干燥箱/烘箱', '干燥箱': '干燥箱/烘箱',
    '真空干燥箱': '干燥箱/烘箱', '烘箱': '干燥箱/烘箱', '烘干箱': '干燥箱/烘箱',
    '培养箱': '培养箱', '恒温培养箱': '培养箱', '生化培养箱': '培养箱',
    '霉菌培养箱': '培养箱', '二氧化碳培养箱': '培养箱', 'CO2培养箱': '培养箱',
    '人工气候箱': '培养箱', '光照培养箱': '培养箱', '恒温恒湿箱': '培养箱',
    '恒温恒湿试验箱': '培养箱', '厌氧培养箱': '培养箱', '振荡培养箱': '培养箱',
    # -- 清洗与灭菌 --------------------------------------------------------
    '超声波清洗机': '超声波清洗机', '超声波清洗器': '超声波清洗机',
    '超声清洗机': '超声波清洗机', '超声波清洗': '超声波清洗机',
    '灭菌器': '灭菌器/灭菌锅', '灭菌锅': '灭菌器/灭菌锅', '灭菌柜': '灭菌器/灭菌锅',
    '高压灭菌锅': '灭菌器/灭菌锅', '高压蒸汽灭菌器': '灭菌器/灭菌锅',
    '蒸汽灭菌器': '灭菌器/灭菌锅', '消毒器': '灭菌器/灭菌锅',
    '洗瓶机': '清洗/消毒', '消毒柜': '清洗/消毒', '紫外消毒': '清洗/消毒',
    # -- 洁净与防护 --------------------------------------------------------
    '生物安全柜': '生物安全柜',
    '超净工作台': '超净工作台/洁净台', '洁净工作台': '超净工作台/洁净台',
    '净化工作台': '超净工作台/洁净台', '超净台': '超净工作台/洁净台',
    '洁净台': '超净工作台/洁净台', '洁净棚': '超净工作台/洁净台',
    '通风柜': '通风柜', '通风橱': '通风柜', '排风罩': '通风柜', '通风罩': '通风柜',
    # -- 温控与混匀 --------------------------------------------------------
    '水浴锅': '水浴锅/恒温槽', '水浴槽': '水浴锅/恒温槽', '恒温水浴': '水浴锅/恒温槽',
    '油浴锅': '水浴锅/恒温槽', '恒温槽': '水浴锅/恒温槽', '低温恒温槽': '水浴锅/恒温槽',
    '恒温循环': '水浴锅/恒温槽', '恒温水槽': '水浴锅/恒温槽', '水槽': '水浴锅/恒温槽',
    '冷却液循环泵': '水浴锅/恒温槽', '循环冷却器': '水浴锅/恒温槽',
    '恒温金属浴': '金属浴/恒温器', '金属浴': '金属浴/恒温器',
    '干式恒温器': '金属浴/恒温器', '恒温器': '金属浴/恒温器', '恒温仪': '金属浴/恒温器',
    '加热模块': '金属浴/恒温器', '恒温箱': '金属浴/恒温器',
    '振荡器': '振荡器/摇床/混匀', '摇床': '振荡器/摇床/混匀', '恒温摇床': '振荡器/摇床/混匀',
    '涡旋混匀仪': '振荡器/摇床/混匀', '旋涡混合器': '振荡器/摇床/混匀',
    '涡旋仪': '振荡器/摇床/混匀', '混匀仪': '振荡器/摇床/混匀',
    '漩涡混合器': '振荡器/摇床/混匀', '混合仪': '振荡器/摇床/混匀',
    '混匀器': '振荡器/摇床/混匀',
    '摇瓶机': '振荡器/摇床/混匀', '脱色摇床': '振荡器/摇床/混匀',
    # -- 破碎与研磨 --------------------------------------------------------
    '匀浆机': '匀浆/研磨', '匀浆器': '匀浆/研磨', '组织匀浆': '匀浆/研磨',
    '均质器': '匀浆/研磨', '分散机': '匀浆/研磨', '乳化机': '匀浆/研磨',
    '研磨仪': '匀浆/研磨', '球磨机': '匀浆/研磨', '粉碎机': '匀浆/研磨',
    '组织研磨': '匀浆/研磨', '超声破碎仪': '匀浆/研磨',
    # -- 分析与检测 --------------------------------------------------------
    '分光光度计': '分光光度计/光谱', '紫外可见分光光度计': '分光光度计/光谱',
    '荧光分光光度计': '分光光度计/光谱', '原子吸收分光光度计': '分光光度计/光谱',
    '原子荧光光度计': '分光光度计/光谱', '红外光谱仪': '分光光度计/光谱',
    '紫外分光光度计': '分光光度计/光谱', '可见分光光度计': '分光光度计/光谱',
    '紫外可见光度计': '分光光度计/光谱',
    '超微量分光光度计': '分光光度计/光谱', '核酸蛋白测定仪': '分光光度计/光谱',
    '色谱仪': '色谱仪', '色谱': '色谱仪', '液相色谱': '色谱仪', '气相色谱': '色谱仪',
    '离子色谱': '色谱仪', '薄层色谱': '色谱仪', '质谱仪': '色谱仪',
    '酶标仪': '酶标仪', '酶标分析仪': '酶标仪', '洗板机': '酶标仪',
    '微孔板检测仪': '酶标仪',
    '熔点仪': '熔点仪', '熔点测定仪': '熔点仪',
    '旋转蒸发仪': '旋转蒸发仪', '旋转蒸发器': '旋转蒸发仪', '旋蒸': '旋转蒸发仪',
    '旋光仪': '物性/理化测定', '折光仪': '物性/理化测定', '阿贝折射仪': '物性/理化测定',
    '浊度计': '物性/理化测定', '浊度仪': '物性/理化测定', '粘度计': '物性/理化测定',
    '黏度计': '物性/理化测定', '密度计': '物性/理化测定', '张力仪': '物性/理化测定',
    '水质分析仪': '水质/环境检测', 'COD测定仪': '水质/环境检测',
    '溶解氧测定仪': '水质/环境检测', '总有机碳分析仪': '水质/环境检测',
    'BOD测定仪': '水质/环境检测',
    '水分测定仪': '样品前处理', '水分快速测定仪': '样品前处理', '消解仪': '样品前处理',
    '微波消解': '样品前处理',
    '氮吹仪': '样品前处理', '固相萃取': '样品前处理', '萃取仪': '样品前处理',
    '定氮仪': '样品前处理', '凯氏定氮': '样品前处理', '浓缩仪': '样品前处理',
    '赶酸仪': '样品前处理',
    # -- 分子与细胞 --------------------------------------------------------
    'PCR仪': 'PCR仪', '基因扩增仪': 'PCR仪', '梯度PCR仪': 'PCR仪',
    '实时荧光定量PCR': 'PCR仪', '定量PCR': 'PCR仪', '扩增仪': 'PCR仪',
    '电泳仪': '电泳/凝胶成像', '电泳槽': '电泳/凝胶成像', '电泳电源': '电泳/凝胶成像',
    '电泳': '电泳/凝胶成像',
    '水平电泳槽': '电泳/凝胶成像', '垂直电泳槽': '电泳/凝胶成像',
    '转膜仪': '电泳/凝胶成像', '电穿孔仪': '电泳/凝胶成像', '电转仪': '电泳/凝胶成像',
    '凝胶成像': '电泳/凝胶成像', '切胶仪': '电泳/凝胶成像', '紫外分析仪': '电泳/凝胶成像',
    '发光成像': '电泳/凝胶成像', '图像分析系统': '电泳/凝胶成像',
    '细胞计数仪': '细胞/分子生物学', '流式细胞仪': '细胞/分子生物学',
    '细胞破碎仪': '细胞/分子生物学', '生物反应器': '细胞/分子生物学',
    '发酵罐': '细胞/分子生物学',
    # -- 冷藏、真空与前处理设备 --------------------------------------------
    '超低温冰箱': '冰箱/低温保存箱', '超低温保存箱': '冰箱/低温保存箱',
    '低温保存箱': '冰箱/低温保存箱', '医用冷藏箱': '冰箱/低温保存箱',
    '冷藏箱': '冰箱/低温保存箱', '冷冻箱': '冰箱/低温保存箱',
    '药品保存箱': '冰箱/低温保存箱', '医用冰箱': '冰箱/低温保存箱',
    '低温冰箱': '冰箱/低温保存箱', '血液冷藏箱': '冰箱/低温保存箱',
    '冰柜': '冰箱/低温保存箱', '冰箱': '冰箱/低温保存箱',
    '制冰机': '制冰机', '雪花制冰机': '制冰机',
    '碎冰机': '制冰机',
    '液氮罐': '液氮罐', '液氮容器': '液氮罐', '液氮生物容器': '液氮罐',
    '冷冻干燥机': '冷冻干燥机', '冻干机': '冷冻干燥机', '真空冷冻干燥': '冷冻干燥机',
    '真空泵': '真空泵', '旋片泵': '真空泵', '隔膜泵': '真空泵',
    '循环水真空泵': '真空泵', '油封旋片泵': '真空泵', '真空机组': '真空泵',
    # -- 实验台件与其它 ----------------------------------------------------
    '移液器': '移液器', '移液枪': '移液器', '微量移液器': '移液器',
    '加样枪': '移液器', '分液器': '移液器', '瓶口分液器': '移液器',
    '显微镜': '显微镜', '生物显微镜': '显微镜', '体视显微镜': '显微镜',
    '荧光显微镜': '显微镜', '倒置显微镜': '显微镜', '金相显微镜': '显微镜',
    '偏光显微镜': '显微镜', '数码显微镜': '显微镜',
    '反应釜': '反应釜', '水热合成反应釜': '反应釜', '高压反应釜': '反应釜',
    '光合作用测定仪': '植物/光合测定', '叶绿素测定仪': '植物/光合测定',
    '叶面积测量仪': '植物/光合测定', '茎杆强度测定仪': '植物/光合测定',
    '种子发芽箱': '植物/光合测定', '植物生长箱': '植物/光合测定',
    '切片机': '病理/切片', '石蜡切片机': '病理/切片', '包埋机': '病理/切片',
    '染色机': '病理/切片', '脱水机': '病理/切片', '摊烤片机': '病理/切片',
    '烤片机': '病理/切片',
    '离子计': '物性/理化测定', 'ORP计': '水质/环境检测',
    '植物营养测定仪': '植物/光合测定',
}

#: 列表页「状态」列里说明**这单没有真实成交**的词。这些单一行价格都不产出。
TERMINAL_STATE_WORDS = ('终止', '取消', '重新竞价', '废标', '流标', '失败', '作废', '未成交')

#: 型号列里这些值不是型号。
PLACEHOLDER_MODELS = frozenset(
    value.lower() for value in
    ('', '-', '—', '/', '无', '其它', '其他', '见附件', '详见附件', '见清单', '详见清单',
     '无型号', '不适用', 'N/A', 'none', 'null', '待定', '标配'))

_ORDER_TEMPLATE = 'OrderResultPublicityView.aspx'
_BILL_TEMPLATE = 'ResultPublicityView.aspx'

# --- HTML 小工具（与产品同构：属性值里的 ``>`` 不是标签结束） --------------------
_TAG_SHAPE = r'(?:[^>"\']|"[^"]*"|\'[^\']*\')*'
_LIST_ROW = re.compile(
    r"<div class=\"list-content\"[^>]*?\scode='([^']*)'[^>]*?\scodeType='([^']*)'"
    r"[^>]*?\scode1='([^']*)'[^>]*?code2\s*=\s*'([^']*)'[^>]*?>(.*?)</div>\s*</div>", re.S)
_DIV_CELL = re.compile(r'<div[^>]*>(.*?)</div>', re.S)
_TABLE = re.compile(r'(?is)<table\b' + _TAG_SHAPE + r'>(.*?)</table\s*>')
_TR = re.compile(r'(?is)<tr\b' + _TAG_SHAPE + r'>(.*?)</tr\s*>')
_TD = re.compile(r'(?is)<t[dh]\b' + _TAG_SHAPE + r'>(.*?)</t[dh]\s*>')
_TAG = re.compile(r'(?is)<' + _TAG_SHAPE + r'>')
_PAGES = re.compile(r'ANP_checkInput\([^,]*,\s*(\d+)\s*\)')
#: 分页控件自己印的总数：「记录总数：<font…><b>640</b></font> 总页数：<font…><b>64</b></font>
#: 当前页：<font…><b>1</b></font>」。
#:
#: ⚠️ **别拿 ``ANP_checkInput(bid,mv)`` 里的 ``mv`` 当总页数**。实测：同一个查询的分页
#: 控件印着「总页数：64」，而 ``ANP_checkInput`` 的 ``mv`` 是 **6**，两个数对不上（``mv``
#: 到底代表什么我没验证到）。所以页数只认控件文字，``_PAGES`` 只在文字缺失时兜底
#: （实测那条分支从没走到过）。``设计规范`` 用 ``ANP_checkInput(...,78)`` 估过"78 页 ×
#: 15 条"，它自己标了"推断值"；要确切值就读控件文字。
_TOTALS = re.compile(
    r'记录总数：\s*(?:<[^>]*>\s*)*(\d+)(?:.*?总页数：\s*(?:<[^>]*>\s*)*(\d+))?', re.S)
_CURRENT = re.compile(r'当前页：\s*(?:<[^>]*>\s*)*(\d+)')
_MODES = ('直购', '议价', '竞价', '反拍', '电子反拍', '询价', '比选')
_BREADCRUMB = re.compile(r'(' + '|'.join(_MODES) + r')\s*(?:订单详情|结果详情)')
_SUPPLIER = re.compile(r'(?:中标供应商|供应商|成交供应商)\s*[:：]\s*\|\s*([^\n|]+)')
_ORDER_NO = re.compile(r'\b((?:DD|CM)\d{10,})\b')

#: 表头里认出的列名 → 落库字段名。
_COLUMN_FIELDS = {'商品名称': 'goods_name', '产品名称': 'product_name',
                  '品牌': 'brand', '型号规格': 'model'}


def _text(raw):
    return re.sub(r'\s+', ' ', unescape(_TAG.sub(' ', raw or ''))).strip()


def list_window(today, months=24):
    """时间窗 ``(start, end)``，``YYYY-MM-DD``。列表页吃 ``txtStateDate`` / ``endStateDate``。"""
    end = date(today.year, today.month, today.day)
    year, month = end.year, end.month - months
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1).isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# 列表页
# ---------------------------------------------------------------------------

def page_totals(html):
    """分页控件自己印的 ``{'records','pages','current'}``；印不全的项是 ``None``。"""
    found = _TOTALS.search(html or '')
    totals = {'records': int(found.group(1)) if found else None,
              'pages': int(found.group(2)) if (found and found.group(2)) else None,
              'current': None}
    current = _CURRENT.search(html or '')
    if current:
        totals['current'] = int(current.group(1))
    return totals


def parse_list_page(html):
    """列表页 → ``(rows, total_pages)``。

    每行六列：订单编号／项目名称／采购单位／成交单位／成交时间／状态。
    ``codeType`` 与 ``code2`` 就在行属性里（见 ``设计规范``）。

    页数优先取分页控件印着的「总页数」；没有才退回 ``ANP_checkInput`` 的 ``mv``
    ——那个数**不是**总页数（见 ``_TOTALS`` 的注释），只是最后的兜底。
    """
    rows = []
    for code, code_type, code1, code2, inner in _LIST_ROW.findall(html or ''):
        # 行正则的 ``(.*?)</div>\s*</div>`` 会在**最后一格的闭合 ``</div>`` 之前**收尾，
        # 所以 ``inner`` 里的最后一个 ``<div>`` 是没闭合的。补一个闭合标签，六列才齐
        # （订单编号／项目名称／采购单位／成交单位／成交时间／状态）。
        cells = [cell for cell in (_text(part) for part in _DIV_CELL.findall(inner + '</div>'))
                 if cell]
        if len(cells) < 6:
            continue
        rows.append({
            'bill_guid': code, 'code_type': code_type, 'code1': code1, 'code2': code2,
            'order_no': cells[0], 'project_name': cells[1], 'purchaser': cells[2],
            'supplier': cells[3], 'deal_time': cells[4], 'state': cells[5],
        })
    totals = page_totals(html)
    if totals['pages'] is None:
        found = _PAGES.search(html or '')
        totals['pages'] = int(found.group(1)) if found else 1
    return rows, totals['pages']


def detail_template(code_type, code2):
    """按 ``codeType`` / ``code2`` 选详情页模板；**认不出来返回 ``None``**。

    判据与产品自己的 ``_detail_template`` 是同一个：服务类（``code2=1``）那两套
    FW 模板没有样本、没验证过，所以**不猜**——返回 ``None``、不发这条请求。
    """
    return PLATFORM._detail_template(code_type, code2)


def detail_url(row):
    template = detail_template(row['code_type'], row['code2'])
    if template is None:
        return None
    return PLATFORM.detail_url.format(template=template, id=row['bill_guid'],
                                      kind=row['code_type'], code=row['code1'])


def is_terminal_state(state):
    return any(word in (state or '') for word in TERMINAL_STATE_WORDS)


def normalize_deal_time(raw):
    """``2026/9/23 14:24:58`` → ``('2026-09-23', '2026-09-23 14:24:58')``。"""
    found = re.match(r'(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})(?:\s+(\d{1,2}:\d{2}(?::\d{2})?))?',
                     (raw or '').strip())
    if not found:
        return '', ''
    stamp = f'{int(found.group(1)):04d}-{int(found.group(2)):02d}-{int(found.group(3)):02d}'
    return stamp, (f'{stamp} {found.group(4)}' if found.group(4) else stamp)


def order_mode(cleaned_body):
    """这一单是什么单据：直购／议价／竞价／反拍。页面面包屑自己印着。"""
    found = _BREADCRUMB.search(cleaned_body or '')
    return found.group(1) if found else ''


# ---------------------------------------------------------------------------
# 详情页
# ---------------------------------------------------------------------------

def goods_table(html):
    """详情页商品信息表 → ``(header, rows)``；认不出表返回 ``(None, [])``。

    判据与产品一致：表头里同时有「型号规格」和任一个价格列名
    （``金额（元）``/``协议价格（元）``/``中标价``）。
    """
    for table in _TABLE.findall(html or ''):
        header, rows = None, []
        for tr in _TR.findall(table):
            cells = [_text(cell) for cell in _TD.findall(tr)]
            if not any(cells):
                continue
            if header is None:
                if '型号规格' in cells and any(
                        name in cells for name in PLATFORM._ALL_PRICE_COLUMNS):
                    header = cells
                continue
            if len(cells) > len(header):
                # 比表头还宽的行属于别的表（例如更长的报价明细），不是本表的行。
                continue
            rows.append(cells + [''] * (len(header) - len(cells)))
        if header is not None:
            return header, rows
    return None, []


def select_prices(header, rows, total):
    """整单一次判断：选哪一列是成交单价。

    规则与产品的 ``_quote_rows`` 同一套（**订单级自校验**：Σ单价×数量 必须等于
    整单金额），只是这里保留"这一格是从哪一列读的"，因为直购单与议价单的性质不同。
    反拍页走 ``_BILL_PRICE_COLUMNS``（只有「中标价」）——**「最高限价」不在候选里**。

    返回 ``[(row_index, column, unit_price), …]``；认不出或对不上返回 ``None``。
    """
    def at(name):
        return header.index(name) if name in header else None

    model_at, quantity_at = at('型号规格'), at('数量')
    if model_at is None or quantity_at is None or total is None:
        return None
    orders = ((PLATFORM._BILL_PRICE_COLUMNS,) if '中标价' in header
              else (PLATFORM._PRICE_COLUMNS, tuple(reversed(PLATFORM._PRICE_COLUMNS))))
    for columns in orders:
        picked, summed = [], 0.0
        for index, cells in enumerate(rows):
            model = cells[model_at].strip()
            if not model:
                continue
            try:
                quantity = float(cells[quantity_at].replace(',', ''))
            except ValueError:
                picked = []
                break
            if quantity <= 0:
                picked = []
                break
            chosen = None
            for column in columns:
                where = at(column)
                if where is None:
                    continue
                raw = cells[where].replace(',', '')
                if re.fullmatch(r'\d+(?:\.\d+)?', raw) and float(raw) > 0:
                    chosen = (column, float(raw))
                    break
            if chosen is None:
                picked = []
                break
            picked.append((index, chosen[0], chosen[1]))
            summed += chosen[1] * quantity
        if picked and abs(summed - total) <= 0.01:
            return picked
    return None


def no_price_reason(header, rows, total):
    """这一单为什么选不出成交单价——**三种原因分开说**，不许都写成"没有价"。

    实测近 60 个月窗口里 ``no_price`` 有 1,252 单，真实成因是两类：
    **466 单**是状态还在「反拍／重新反拍」的进行中单（页面列名在、数据行是空的），
    **786 单**是「交易成功」但**商品表的「型号规格」列整列为空**——那些单的商品名
    一看就是服务性采购（视频拍摄制作、活动策划、图书出版、评价服务），站点把它们
    挂在 ``code2=0``（物品类）的直购单上，所以会走进这条解析路径。

    选价函数要求每行都有型号（没有型号的行直接跳过），所以第二种情况必然选不出价；
    把两者写成同一句话，读的人会以为解析退化了。
    """
    if total is None:
        return '页面没有印整单成交金额'
    if '型号规格' not in header:
        return '商品表里没有「型号规格」列'
    model_at = header.index('型号规格')
    if not any(cells[model_at].strip() for cells in rows if len(cells) > model_at):
        return ('商品表的「型号规格」列整列为空（这类单是服务性采购挂在物品类'
                'code2=0 的直购单上，没有型号可取）')
    return 'Σ(单价×数量) 与页面印的整单金额对不上'


def award_total(cleaned_body):
    """页面自己印着的整单成交金额；没有返回 ``None``。复用产品那一套判据。"""
    return PLATFORM._award_total(cleaned_body or '')


def shape_body(redacted_html, url=''):
    """产品自己的清洗链 + 8093 整形 → ``(清洗正文, 整形正文)``。"""
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    payload = redacted_html.encode('utf-8') if isinstance(redacted_html, str) else redacted_html
    body = PublicWebSearchProvider.decode_body(payload, 'text/html', url)
    return body, PLATFORM.shape_text(body, payload)


def supplier_from_body(cleaned_body):
    found = _SUPPLIER.search(cleaned_body or '')
    return found.group(1).strip() if found else ''


def order_no_from_body(cleaned_body):
    found = _ORDER_NO.search(cleaned_body or '')
    return found.group(1) if found else ''


# --- 隐私闸门 -----------------------------------------------------------------

_PII_PATTERNS = (
    # 手机号要**左右设边界**：站点自己的订单号 CM20180528000206 里藏着 18052800020
    # 这种 11 位串，不设边界会把订单号本身判成手机号，整批页面被闸门拦掉。
    (re.compile(r'(?<![0-9A-Za-z])1[3-9]\d{9}(?![0-9A-Za-z])'), '手机号'),
    (re.compile(r'(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])'), '身份证/税号'),
    (re.compile(r'(联系人|联系电话|联系地址|收货人|开票信息|纳税人识别号|开户行|银行账号)'
                r'\s*[:：]\s*\S'), '个人信息字段带值'),
    (re.compile(r'[\w.+-]+@[\w-]+\.[\w.]{2,}'), '邮箱'),
)


def pii_residue(text):
    """落盘前最后一道闸门：返回命中的类别列表，空表示干净。"""
    return [label for pattern, label in _PII_PATTERNS if pattern.search(text or '')]


def redact(html_bytes):
    """产品自己的脱敏钩子 → ``(脱敏文本, 残留命中)``。"""
    redacted = redact_detail(PLATFORM_KEY, html_bytes)
    if isinstance(redacted, bytes):
        redacted = redacted.decode('utf-8', 'replace')
    return redacted, pii_residue(redacted)


# ---------------------------------------------------------------------------
# 类别
# ---------------------------------------------------------------------------

def assign_category(*texts):
    """按商品自己的文字（商品名称／产品名称／型号／品牌／项目名）定类别。

    取**命中的最长类别词**，所以「超纯水机」不会被更短的「纯水机」抢走、
    「分析天平」不会被「天平」抢走；一个都没命中时返回 ``None``。

    匹配**大小写不敏感**：站点的商品名里 ``PH计`` 与 ``pH计`` 都出现过，按原样比
    会把前一种漏掉（实测旧库 1,634 行里这样漏了 5 行）。
    """
    haystack = ' '.join(text or '' for text in texts).lower()
    best = ''
    for keyword in CATEGORY_OF:
        if keyword.lower() in haystack and len(keyword) > len(best):
            best = keyword
    return CATEGORY_OF.get(best) if best else None


# ---------------------------------------------------------------------------
# 验证
# ---------------------------------------------------------------------------

def validate_price_row(item, shaped_body, publication_date):
    """把这一行送进**产品自己的** ``validate_price``，原样返回它的结论。

    送进去的都是页面自己的东西：``excerpt`` 是整形后的那一行
    （``型号规格 | 单价（元） | 数量 | 品牌``），``amount``/``quantity`` 是页面自己
    印的数字。``publication_date`` 用结果公示列表行的「成交时间」——所以它验的是
    "这个日期与列表页印的成交时间一致"，**不是**"详情页正文里有这个日期"。
    """
    from backend.services.procurement_rules import validate_price
    model, brand = item['model'], item['brand']
    raw = {'excerpt': item['price_excerpt'], 'amount': item['unit_price'],
           'amount_unit': '元', 'quantity': item['quantity'], 'model': model,
           'brand': brand, 'kind': 'award', 'amount_scope': 'unit'}
    source = {'body_status': 'fetched', 'body_text': shaped_body, 'url': item['detail_url'],
              'source_type': 'tender_document', 'publication_date': publication_date}
    refusals = {}
    try:
        quote = validate_price(raw, source, {'model': model, 'brand': brand}, refusals)
    except Exception as exc:  # 判据自己抛错也是"没通过"，如实记，不咽掉
        return None, f'{type(exc).__name__}: {exc}'
    return quote, ('' if quote else ','.join(sorted(refusals)) or 'refused')


# ---------------------------------------------------------------------------
# 查价车道接入：按型号直查本地成交明细
# ---------------------------------------------------------------------------

#: 采信为"成交价"的价格列类型。``list_price_direct``（直购单的目录金额）**故意不在**：
#: 那是挂牌价，不是成交价——拿它当基准会把目录价说成成交价（见模块开头第 2 条）。
DEAL_PRICE_KINDS = ('negotiated', 'auction_award')

#: 每条成交价都带上的范围说明：断章取义地拿它当全国行情是这库最容易被误用的方式。
LOCAL_SCOPE_NOTE = ('本条来自广东省教育部门“小零易购”电子采购系统离线成交明细库'
                    '（单一省份、单一渠道的小额采购），是**该平台的成交单价**，'
                    '不代表全国行情。')


def default_database_path() -> Path:
    """本地库位置：``LAB_XLYG_DB`` 优先，默认 ``data/xlyg_prices.sqlite3``。"""
    import os
    return Path(os.getenv('LAB_XLYG_DB')
                or (Path(__file__).resolve().parents[2] / 'data/xlyg_prices.sqlite3'))


def local_price_rows(model, brand=None, *, database_path=None, limit=5):
    """按型号从本地库取成交价行，形状与价格车道里其它来源一致。

    为什么要有它：公告里常常没有目标型号的成交记录（批3 灭菌器那轮 24 条来源全部因
    "正文里没有目标型号"被跳过），而这个库是**按品类词离线全量抓下来**的
    （``scripts/xlyg_crawl.py``），按型号直查才有命中。

    三条口径：

    * 只取 ``price_kind`` 属于 ``DEAL_PRICE_KINDS`` 的行——直购单的目录挂牌价不进来；
    * 型号按 ``identity`` **精确**相等，不拿 SQL 的 LIKE 直接采信（``HVE-50`` 不能
      匹到 ``HVE-500``）；
    * 每一行仍旧走产品自己的 ``validate_price``（与建库时同一套判据），过不了的不返回。

    返回 ``{'rows': [...], 'note': str, 'database': str, 'scanned': int}``。
    ``note`` 非空时调用方要如实记一条缺口——"这条通道这轮没查成"与"查了没有"是两件事。
    """
    from backend.services.procurement_rules import brand_name_matches
    from backend.services.procurement_rules import identity as rule_identity

    path = Path(database_path) if database_path is not None else default_database_path()
    if not path.exists():
        return {'rows': [], 'scanned': 0, 'database': str(path),
                'note': f'本地成交明细库不在（{path}）：按型号直查这条查价通道本轮未生效。'}
    conn = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    # 型号在 SQL 里就用 LIKE 粗筛（库里 6000+ 条成交价行，按日期倒序取前 N 条根本
    # 扫不到目标型号——实测 HVE-50 一条都不在前 200 行里），但**采信前仍按
    # ``identity`` 精确比一次**：LIKE 只是缩小范围，不当匹配用。型号里的
    # ``%``／``_`` 是 LIKE 的通配符，先转义掉再拼。
    like = ('%' + str(model).strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
    try:
        placeholders = ','.join('?' * len(DEAL_PRICE_KINDS))
        fetched = conn.execute(
            "SELECT order_no, brand, model, quantity, unit_price, price_kind, deal_date, "
            "purchaser, supplier, detail_url, table_header, price_excerpt, excerpt "
            "FROM xlyg_item WHERE verified = 1 AND model <> '' "
            "AND price_kind IN (%s) AND model LIKE ? ESCAPE '\\' "
            "ORDER BY deal_date DESC LIMIT ?" % placeholders,
            (*DEAL_PRICE_KINDS, like, max(20, int(limit) * 20))).fetchall()
    finally:
        conn.close()

    want_model = rule_identity(model)
    want_brand = rule_identity(brand) if brand else ''
    produced, scanned, brand_skipped = [], 0, 0
    for row in fetched:
        item = dict(row)
        if not want_model or rule_identity(item.get('model')) != want_model:
            continue
        scanned += 1
        if want_brand and item.get('brand'):
            library_brand = str(item['brand'])
            # 库里的品牌写法比录入侧长（``梅特勒托利多METTLERTOLEDO``、
            # ``梅特勒-托利多(METTLER TOLEDO)``）：先走项目的品牌归一，再退一步做
            # "库里的写法包含目标品牌"的包含判断——两条都不过才算品牌对不上。
            if (not brand_name_matches(library_brand, str(brand))
                    and want_brand not in rule_identity(library_brand)):
                brand_skipped += 1
                continue
        unit_price = float(item.get('unit_price') or 0)
        if unit_price <= 0:
            continue
        produced.append({
            'brand': item.get('brand') or '',
            'model': item.get('model') or '',
            'procurement_unit': item.get('purchaser') or '',
            # 库里没有技术参数（见模块头：抓的是成交商品表），配置这一格留给产品页补；
            # 补不到就保持 unknown，绝不用别的型号的参数顶替。
            'configuration': 'unknown',
            'kind': 'award',
            'amount': unit_price, 'amount_unit': '元', 'amount_scope': 'unit',
            'quantity': item.get('quantity'), 'currency': 'CNY',
            'date': str(item.get('deal_date') or ''),
            'project_id': item.get('order_no') or '',
            'supplier': item.get('supplier') or '',
            'lot': '', 'item': item.get('goods_name') or '',
            'tax': 'unknown', 'installation': 'unknown', 'warranty': 'unknown',
            'shipping': 'unknown', 'service_response': 'unknown',
            'service_onsite': 'unknown', 'delivery': 'unknown',
            # 引文用页面那一行自己的原文（库建的时候逐字比对过）。
            'excerpt': str(item.get('excerpt') or item.get('price_excerpt') or ''),
            'url': item.get('detail_url') or '',
            'channel': 'xlyg_local_db',
            'price_kind': item.get('price_kind') or '',
            'local_db': str(path),
            'scope_note': LOCAL_SCOPE_NOTE,
            'unit_price': unit_price,
            'calculation': f'{unit_price} × 1',
            'level': 'historical_configuration',
            'comparison_level': 'historical_configuration',
            'date_verified': True,
            'gaps': ['configuration', 'tax', 'installation', 'warranty', 'delivery'],
        })
        if len(produced) >= max(1, int(limit)):
            break
    return {'rows': produced, 'scanned': scanned, 'brand_skipped': brand_skipped,
            'database': str(path), 'note': ''}


# ---------------------------------------------------------------------------
# 候选发现接入：按品类取"同品类、别的品牌"的真实成交型号
# ---------------------------------------------------------------------------

#: 型号列里写的其实是**外形尺寸**：``1000×600×850``、``1500*1400*1200mm``、
#: ``1200*700*1600 /全钢标准型``。这类行在库里出在"洁净工作台／实验台"的钢木家具单上
#: ——商品名称写的是品类，型号列印的是尺寸。实测三条：
#: ``DD20201216000003``（``goods_name=洁净工作台``、``model=1600×800×850/钢木结构``）、
#: ``CM20251226000567``（``goods_name=水槽/三联水龙头``、``model=1000*1000*850``）、
#: ``FPCM20260128000005_1``（``goods_name=角柜``）。尺寸不是型号，拿它去搜产品页
#: 只会白花检索额度，所以在取材这一层就挡掉。
_DIMENSION_MODEL = re.compile(
    r'[-+]?\d+(?:\.\d+)?\s*(?:[*×xX]\s*[-+]?\d+(?:\.\d+)?\s*){1,2}(?:mm|cm|m)?', re.I)

#: 落在"中文说明"上的字符。判尺寸型号时先把它们抹掉：``2000×600×850/带箱体/钢木结构``
#: 抹掉中文后只剩那串尺寸。
_CJK = re.compile(r'[\u3400-\u9fff\uf900-\ufaff]+')

#: 品类词先按这个长度过滤：一个字（"柜""台"）会把整库捞成一锅。
_MIN_CATEGORY_CHARS = 2


def _is_dimension_model(model) -> bool:
    """型号列写的是不是"尺寸"。

    判据只有一条：把中文说明和首尾分隔符去掉之后，**整格**就是一串用 ``×``/``*``/``x``
    连起来的数字（可带 ``mm``/``cm``/``m``）。``5425``（艾本德离心机真型号）没有分隔符，
    不匹配；``DK-98-Ⅱ单列两孔``、``B-260型``、``LS-75HD(原LS-B75L-I数显）`` 抹掉中文后
    还剩字母，也不匹配。
    """
    stripped = _CJK.sub(' ', str(model or '')).strip()
    stripped = stripped.strip(' \t/、,，;；')
    if not stripped:
        return True
    return _DIMENSION_MODEL.fullmatch(stripped) is not None


def _strip_category_suffix(brand, product_name) -> str:
    """去掉品牌列后面粘着的**品目**：``海尔(其他教学用仪器)`` → ``海尔``。

    判据不是"见到括号就删"——只在括号里的那句话**恰好等于这一行的 ``product_name``**
    时才删（实测 ``CM20260826000078`` 第 12 行：``brand=海尔(其他教学用仪器)``、
    ``product_name=其他教学用仪器``；同一单第 5、10 行是尼康、知楚同一个写法）。
    ``华志（福建）``、``梅特勒-托利多(METTLER TOLEDO)``、``实验室家具(广州赛亚)``
    的括号里不是这一行的品目，原样保留。
    """
    text = str(brand or '').strip()
    name = str(product_name or '').strip()
    if not text or not name:
        return text
    for left, right in (('(', ')'), ('（', '）')):
        suffix = left + name + right
        if text.endswith(suffix):
            return text[:-len(suffix)].strip()
    return text


def local_candidate_leads(category_words, *, exclude_models=(), exclude_brands=(),
                          limit=8, database_path=None):
    """按品类从本地库取"同品类、别的品牌"的成交型号，作为**候选发现**的素材。

    为什么要有它（2026-10-05）：批3 的候选有时一条都提不出来（模型没提、正文直读也没
    捞到），而库里躺着 12000+ 条真实成交明细——水浴 19、天平 53、灭菌 54、洁净台 45 条
    唯一（品牌，型号）成交价行。这些是**真实成交过的同品类机器**，比凭空提名靠谱；
    给它们一条线索，后面 ``fetch_candidate_detail_pages`` 与
    ``search_candidate_product_pages`` 会照常去给每一台找产品页，配置那条路也一并打开。

    几条口径（都在这一层定，调用方不再放宽）：

    * **品类只认商品名**：按 ``goods_name``／``product_name`` 匹配，**不拿 ``model``
      匹配**。实测（2026-10-05，11 个品类词）"只写在型号列里"的行一条也没有，
      而拿型号匹配会把 ``goods_name=旋光管``、``model=长20cm,带水浴夹套`` 这类
      整行捞进来；库里 ``category`` 列大半为空，不能拿它当唯一依据。
    * **排除基线品牌与已在列型号**，要的是**跨品牌**候选（同品牌由基线那条线负责）；
      品牌比对同时走 ``brand_identity`` 与 ``brand_name_matches``（``上海一恒`` 与
      ``一恒``、``申安`` 与 ``上海申安`` 在库里是两种写法）。
    * 只取成交价（``DEAL_PRICE_KINDS``）且 ``verified=1``；尺寸当型号的行和占位型号在
      取材时就挡掉（见 ``_is_dimension_model``）。
    * **一个品牌只出一条**，取它最近成交的那个型号。这条是对早先"每个（品牌，型号）留
      一条"的收紧：实测灭菌器按（品牌，型号）取前 6 条只覆盖 4 个品牌（申安两条、
      滨江医疗两条），而候选这一格的用处正是"**别的品牌**能替代的机器"；同一家的第二个
      型号换不来新的可比对象。型号写法**原样保留**（``B-260型``、``DK-98-ⅡA双列六孔``），
      不做清洗——库里的写法是原文；它带的中文说明由下游的
      ``procurement_rules.model_spellings`` 在**匹配**那一步去掉，候选自己显示的写法不变。

    返回 ``{'leads': [...], 'scanned': int, 'database': str, 'note': str}``，
    ``leads`` 的形状与正文直读线索兼容（交给 ``merge_discovered_candidates`` 合并去重）。
    ``note`` 非空时调用方要如实记一条缺口——"这条通道这轮没查成"与"查了没有"是两件事。
    """
    from backend.services.procurement_rules import brand_identity, brand_name_matches
    from backend.services.procurement_rules import identity as rule_identity

    path = Path(database_path) if database_path is not None else default_database_path()
    words = [str(word).strip() for word in (category_words or [])
             if len(str(word or '').strip()) >= _MIN_CATEGORY_CHARS]
    result = {'leads': [], 'scanned': 0, 'database': str(path), 'note': ''}
    if not words:
        result['note'] = ('品类词缺省或太短（少于 %d 个字）：按品类补候选这条通道本轮未生效。'
                          % _MIN_CATEGORY_CHARS)
        return result
    if not path.exists():
        result['note'] = f'本地成交明细库不在（{path}）：按品类补候选这条通道本轮未生效。'
        return result

    excluded_models = {rule_identity(model) for model in exclude_models if model}
    excluded_brands = [str(brand) for brand in exclude_brands if str(brand or '').strip()]
    wanted = max(1, int(limit))
    conn = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ','.join('?' * len(DEAL_PRICE_KINDS))
        seen, seen_brands, leads = set(), set(), []
        for word in words:
            like = '%' + word.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            # 取窗口比想要的条数宽：同一个（品牌，型号）在库里重复很多（灭菌器 79 行里
            # 只有 54 个唯一型号），窗口太窄会被重复行占满。
            rows = conn.execute(
                "SELECT brand, model, product_name, goods_name, unit_price, price_kind, "
                "deal_date, detail_url, excerpt, purchaser, order_no "
                "FROM xlyg_item WHERE verified = 1 AND model <> '' "
                f"AND price_kind IN ({placeholders}) "
                "AND (goods_name LIKE ? ESCAPE '\\' OR product_name LIKE ? ESCAPE '\\') "
                "ORDER BY deal_date DESC LIMIT ?",
                (*DEAL_PRICE_KINDS, like, like, max(60, wanted * 8))).fetchall()
            result['scanned'] += len(rows)
            for row in rows:
                item = dict(row)
                model = str(item.get('model') or '').strip()
                if not model or model.lower() in PLACEHOLDER_MODELS:
                    continue
                if _is_dimension_model(model):
                    continue
                brand = _strip_category_suffix(item.get('brand'), item.get('product_name'))
                key = (brand_identity(brand), rule_identity(model))
                if not key[1] or key in seen:
                    continue
                if key[1] in excluded_models:
                    continue
                if key[0] and any(key[0] == brand_identity(other)
                                  or brand_name_matches(brand, other)
                                  for other in excluded_brands):
                    continue
                if key[0] and key[0] in seen_brands:
                    # 一个品牌只出一条：候选要的是"别的品牌能替代的机器"。
                    continue
                seen.add(key)
                if key[0]:
                    seen_brands.add(key[0])
                leads.append({
                    'brand': brand,
                    'model': model,
                    'configuration': '',
                    'url': item.get('detail_url') or '',
                    # 引文用页面那一行自己的原文（建库时逐字比对过），不是拼出来的。
                    'excerpt': str(item.get('excerpt') or ''),
                    'proposed_by': 'local_db',
                    'note': ('这一条来自本地成交明细库（广东省教育部门“小零易购”电子采购'
                             '系统的离线成交明细）：是**同品类、别的品牌**的真实成交型号。'
                             '库里没有技术参数，配置要靠这台机器自己的产品页去取，取不到不写。'),
                    'library': {
                        'channel': 'xlyg_local_db',
                        'database': str(path),
                        'order_no': item.get('order_no') or '',
                        'goods_name': item.get('goods_name') or '',
                        'price_kind': item.get('price_kind') or '',
                        'unit_price': item.get('unit_price'),
                        'deal_date': str(item.get('deal_date') or ''),
                        'purchaser': item.get('purchaser') or '',
                        'scope_note': LOCAL_SCOPE_NOTE,
                    },
                })
                if len(leads) >= wanted:
                    break
            if len(leads) >= wanted:
                break
    finally:
        conn.close()
    result['leads'] = leads
    return result


def cross_check(shaped_section, picked, header, rows):
    """两套彼此独立的解析比对：``(型号, 单价, 数量)`` 三元组必须一致。

    一套是本模块**按 HTML 单元格**解析的 ``picked``，另一套是产品**按清洗后文本**
    解析的 ``shaped_section``。对不上就记缺口，不静默采信任何一边。
    """
    def num(text):
        try:
            return float(str(text).replace(',', ''))
        except (TypeError, ValueError):
            return None

    model_at, quantity_at = header.index('型号规格'), header.index('数量')
    mine = sorted((rows[index][model_at].strip(), price, num(rows[index][quantity_at]))
                  for index, _column, price in picked or [])
    theirs = []
    for line in shaped_section or []:
        cells = [cell.strip() for cell in line.split('|')]
        if cells[0] == '型号规格':
            continue
        if len(cells) < 3:
            return False, f'产品整形行不足三格: {line!r}'
        theirs.append((cells[0], num(cells[1]), num(cells[2])))
    return mine == sorted(theirs), f'mine={mine} theirs={sorted(theirs)}'


# ---------------------------------------------------------------------------
# 建库
# ---------------------------------------------------------------------------

def schema_sql():
    return SCHEMA_PATH.read_text(encoding='utf-8')


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(schema_sql())
    return conn


#: 漏斗各阶段的名字，顺序即处理顺序。
#:
#: ``skipped_service`` 与 ``skipped_no_template`` **必须分开**：前者是"服务类（``code2=1``），
#: 产品裁定不接"，后者是"这个模板没见过、不敢猜"。全量遍历一个窗口就有几千条服务类订单，
#: 混在一起会让人以为漏了一大块数据。
#:
#: ``merged_duplicate_order_no`` 记的是**同一个订单号出现在多个 ``bill_guid`` 上**的合并
#: （实测近 60 个月窗口里有 7 例：一单先"终止采购"、同一订单号后来又重新发布并成交）。
#: 订单表以订单号为主键，所以只能留一条；留哪条有固定规则（见 ``merge_duplicate_order_numbers``），
#: 被合并掉的那条**整条记进缺口表**，不静默丢。
FUNNEL_STAGES = ('list_rows', 'unique_orders', 'merged_duplicate_order_no',
                 'skipped_no_template', 'skipped_service',
                 'skipped_terminal', 'detail_fetched', 'detail_ok', 'orders_with_price',
                 'goods_rows_raw', 'rows_kept', 'rows_validate_price')


def merge_duplicate_order_numbers(by_guid, funnel, gaps):
    """同一个订单号有多个 ``bill_guid`` 时，只留一条，**另一条整条记进缺口表**。

    订单表以 ``order_no`` 为主键（`xlyg_schema.sql`），所以同号多单只能留一条——但"悄悄
    少一行"是不允许的，落盘行数必须等于内存结果的行数。实测近 60 个月窗口里有 **7 例**，
    全是同一单先「终止采购」、同一订单号后来又重新发布并「反拍成功」。

    留哪条的规则（固定、可复算）：**非终止状态优先**；都非终止时**成交时间晚的优先**
    （后发布的那一次才是真结果）；仍并列时按 ``bill_guid`` 排序取第一条，保证可复现。
    """
    groups = {}
    for guid, row in by_guid.items():
        groups.setdefault(row['order_no'], []).append((guid, row))
    merged = {}
    for order_no, entries in groups.items():
        if len(entries) == 1:
            merged[entries[0][0]] = entries[0][1]
            continue
        entries.sort(key=lambda pair: (0 if is_terminal_state(pair[1]['state']) else 1,
                                       pair[1]['deal_time'] or '', pair[0]), reverse=True)
        keep_guid, keep = entries[0]
        merged[keep_guid] = keep
        for guid, row in entries[1:]:
            funnel['merged_duplicate_order_no'] += 1
            gaps.append({
                'order_no': order_no, 'kind': 'duplicate_order_no',
                'detail': (f'同一订单号还有 bill_guid={guid}（状态「{row["state"]}」，'
                           f'成交时间 {row["deal_time"]}）；本库保留 '
                           f'bill_guid={keep_guid}（状态「{keep["state"]}」，'
                           f'成交时间 {keep["deal_time"]}），另一条不重复计数')})
    return merged


def build(orders, details, *, window, built_at=None, keywords=PROBE_KEYWORDS):
    """把抓取结果读成订单表 + 成交明细行（内存结构；落盘由 ``write_database`` 做）。

    ``orders`` 是列表行（可重复：同一个单会被多个品类词搜到），
    ``details`` 是 ``{bill_guid: {'html': 脱敏 HTML, 'status': …, 'note': …}}``。
    """
    funnel = dict.fromkeys(FUNNEL_STAGES, 0)
    funnel['list_rows'] = len(orders)

    by_guid = {}
    for row in orders:
        entry = by_guid.setdefault(row['bill_guid'], {**row, 'keywords': set()})
        entry['keywords'].add(row.get('keyword') or '')
    funnel['unique_orders'] = len(by_guid)

    order_records, item_records, gaps = [], [], []
    by_guid = merge_duplicate_order_numbers(by_guid, funnel, gaps)
    # 落盘用的是 ``order_no`` 主键，所以"内存里有几条"与"库里能落几条"必须是同一个数；
    # 这一行断言就是为了不让上一种情况（同号多单被 REPLACE 掉）重新变哑巴。
    assert len({row['order_no'] for row in by_guid.values()}) == len(by_guid)


    for guid, row in sorted(by_guid.items(), key=lambda pair: pair[1]['deal_time'], reverse=True):
        keywords_of = sorted(k for k in row['keywords'] if k)
        template = detail_template(row['code_type'], row['code2'])
        day, stamp = normalize_deal_time(row['deal_time'])
        record = {
            'bill_guid': guid, 'order_no': row['order_no'], 'code_type': row['code_type'],
            'code1': row['code1'], 'code2': row['code2'], 'template': template or '',
            'order_mode': '', 'project_name': row['project_name'],
            'purchaser': row['purchaser'], 'supplier': row['supplier'],
            'supplier_detail': '', 'state': row['state'], 'deal_date': day,
            'deal_time': stamp, 'award_total': None, 'detail_url': detail_url(row) or '',
            'source_keywords': ','.join(keywords_of), 'fetch_status': '', 'skip_reason': '',
        }
        if template is None:
            if row['code2'] == PLATFORM._SERVICE_CODE2:
                # 服务类是**有意不接**（用户 2026-09-25 裁定：目标是仪器设备的成交价，
                # 服务类采购不是目标数据，见 ``education_platform_sources`` 的注释），
                # 所以单独计数、**不记缺口**——有意跳过写成缺口会让人以为这里漏了数据。
                funnel['skipped_service'] += 1
                record['fetch_status'] = 'skipped'
                record['skip_reason'] = '服务类订单：产品裁定不接（目标是仪器设备的成交价）'
            else:
                funnel['skipped_no_template'] += 1
                record['fetch_status'] = 'skipped'
                record['skip_reason'] = (f'未验证的模板（codeType={row["code_type"]}, '
                                         f'code2={row["code2"]}）：不猜，记缺口跳过')
                gaps.append({'order_no': row['order_no'], 'kind': 'unknown_template',
                             'detail': record['skip_reason']})
            order_records.append(record)
            continue
        if is_terminal_state(row['state']):
            funnel['skipped_terminal'] += 1
            record['fetch_status'] = 'skipped'
            record['skip_reason'] = f'列表状态「{row["state"]}」：没有真实成交'
            order_records.append(record)
            continue

        detail = details.get(guid)
        if detail is None:
            record['fetch_status'] = 'missing'
            record['skip_reason'] = '没有抓取结果'
            order_records.append(record)
            continue
        funnel['detail_fetched'] += 1
        if detail.get('status') != 'ok':
            record['fetch_status'] = detail.get('status') or 'error'
            record['skip_reason'] = detail.get('note') or ''
            gaps.append({'order_no': row['order_no'], 'kind': record['fetch_status'],
                         'detail': record['skip_reason']})
            order_records.append(record)
            continue

        body, shaped = shape_body(detail['html'], record['detail_url'])
        header, rows = goods_table(detail['html'])
        total = award_total(body)
        record.update(order_mode=order_mode(body), supplier_detail=supplier_from_body(body),
                      award_total=total)
        printed = order_no_from_body(body)
        if printed and printed != record['order_no']:
            gaps.append({'order_no': record['order_no'], 'kind': 'order_no_mismatch',
                         'detail': f'详情页印的是 {printed}'})
        if header is None:
            record['fetch_status'] = 'no_table'
            record['skip_reason'] = '详情页没有商品信息表'
            gaps.append({'order_no': row['order_no'], 'kind': 'no_table',
                         'detail': record['skip_reason']})
            order_records.append(record)
            continue
        picked = select_prices(header, rows, total)
        if picked is None:
            record['fetch_status'] = 'no_price'
            record['skip_reason'] = no_price_reason(header, rows, total)
            order_records.append(record)
            continue
        record['fetch_status'] = 'ok'
        funnel['detail_ok'] += 1
        funnel['orders_with_price'] += 1

        section = PLATFORM._quote_section(body)
        agree, why = cross_check(section, picked, header, rows)
        if not agree:
            gaps.append({'order_no': row['order_no'], 'kind': 'cross_check_mismatch',
                         'detail': why})

        keyword_category = CATEGORY_OF.get(row.get('keyword') or '')
        for line_no, (index, column, unit_price) in enumerate(picked, 1):
            cells = rows[index]
            goods = {attr: cells[header.index(name)].strip()
                     for name, attr in _COLUMN_FIELDS.items() if name in header}
            quantity = float(cells[header.index('数量')].replace(',', ''))
            model, brand = goods.get('model', ''), goods.get('brand', '')
            category = assign_category(goods.get('goods_name'), goods.get('product_name'),
                                       model, brand, row['project_name'])
            price_kind = ('auction_award' if column == '中标价'
                          else 'negotiated' if column == '协议价格（元）'
                          else 'list_price_direct')
            item = {
                'order_no': row['order_no'], 'line_no': line_no,
                'goods_name': goods.get('goods_name', ''),
                'product_name': goods.get('product_name', ''),
                'brand': brand, 'model': model, 'quantity': quantity,
                'unit_price': unit_price, 'amount': round(unit_price * quantity, 2),
                'price_column': column, 'price_kind': price_kind,
                'category': category or keyword_category or '',
                'category_source': 'item_text' if category else (
                    'probe_keyword' if keyword_category else ''),
                'deal_date': day, 'purchaser': row['purchaser'],
                'supplier': row['supplier'] or record['supplier_detail'],
                'detail_url': record['detail_url'],
                'excerpt': ' | '.join(cells), 'price_excerpt': '',
                'table_header': ' | '.join(header),
                'source_keywords': record['source_keywords'],
                'order_total_consistent': 1,
            }
            item['price_excerpt'] = ' | '.join([model, f'{unit_price:.2f}',
                                                f'{quantity:.2f}', brand])
            # 验证分三道闸门，逐行算，逐行记是哪一道没过。
            reasons = _structural_reasons(item)
            if reasons:
                item['validate_price'], item['validate_note'] = 0, 'skipped:structural'
            else:
                quote, note = validate_price_row(item, shaped, day)
                item['validate_price'] = 1 if quote else 0
                item['validate_note'] = note
                if not quote:
                    reasons.append(f'validate_price:{note}')
            item['verify_reasons'] = ';'.join(reasons)
            item['verified'] = 0 if reasons else 1
            if item['verified']:
                funnel['rows_kept'] += 1
            if item['validate_price']:
                funnel['rows_validate_price'] += 1
            item_records.append(item)
            funnel['goods_rows_raw'] += 1
        # 抓到价格的那条也要落订单表——否则 /orders/{no} 只查得到失败的单，
        # 成功的单反而"不存在"（第一版就踩了这个坑，测试里钉住）。
        order_records.append(record)

    return {'funnel': funnel, 'orders': order_records, 'items': item_records, 'gaps': gaps,
            'window': list(window), 'keywords': list(keywords),
            'built_at': built_at or datetime.now().isoformat(timespec='seconds')}


def _structural_reasons(item):
    """结构闸门：型号、单价、引文可追溯、隐私。返回没过的那几条。"""
    model = (item['model'] or '').strip()
    reasons = []
    if not model:
        reasons.append('model_empty')
    elif model.lower() in PLACEHOLDER_MODELS:
        reasons.append('model_placeholder')
    if model and item['excerpt'] and model not in item['excerpt']:
        reasons.append('model_not_in_row')
    if not item['unit_price'] > 0:
        reasons.append('price_not_positive')
    if pii_residue(item['excerpt']) or pii_residue(item['price_excerpt']):
        reasons.append('pii_residue')
    return reasons


ITEM_FIELDS = ('order_no', 'line_no', 'goods_name', 'product_name', 'brand', 'model',
               'quantity', 'unit_price', 'amount', 'price_column', 'price_kind',
               'category', 'category_source', 'deal_date', 'purchaser', 'supplier',
               'detail_url', 'excerpt', 'table_header', 'price_excerpt', 'verified',
               'verify_reasons', 'source_keywords', 'order_total_consistent',
               'validate_price')
ORDER_FIELDS = ('order_no', 'bill_guid', 'code_type', 'code2', 'template', 'order_mode',
                'project_name', 'purchaser', 'supplier', 'supplier_detail', 'state',
                'deal_date', 'deal_time', 'award_total', 'detail_url', 'source_keywords',
                'fetch_status', 'skip_reason')


def write_database(path, result):
    """把 ``build`` 的结果写进 SQLite（整库重建，先清空再写）。"""
    conn = connect(path)
    try:
        with conn:
            for table in ('xlyg_item', 'xlyg_order', 'xlyg_gap', 'xlyg_meta'):
                conn.execute(f'DELETE FROM {table}')
            conn.executemany(
                'INSERT OR REPLACE INTO xlyg_order (' + ','.join(ORDER_FIELDS) + ')'
                ' VALUES (' + ','.join('?' * len(ORDER_FIELDS)) + ')',
                [[row.get(field) for field in ORDER_FIELDS] for row in result['orders']])
            conn.executemany(
                'INSERT OR REPLACE INTO xlyg_item ('
                + ','.join(ITEM_FIELDS + ('validate_note',)) + ')'
                ' VALUES (' + ','.join('?' * (len(ITEM_FIELDS) + 1)) + ')',
                [[item.get(field) for field in ITEM_FIELDS] + [item.get('validate_note') or '']
                 for item in result['items']])
            conn.executemany('INSERT INTO xlyg_gap (order_no, kind, detail) VALUES (?,?,?)',
                             [(gap.get('order_no', ''), gap.get('kind', ''), gap.get('detail', ''))
                              for gap in result['gaps']])
            meta = {'window_start': result['window'][0], 'window_end': result['window'][1],
                    'built_at': result['built_at'],
                    'keywords': json.dumps(result['keywords'], ensure_ascii=False),
                    'funnel': json.dumps(result['funnel'], ensure_ascii=False)}
            conn.executemany('INSERT INTO xlyg_meta (key, value) VALUES (?,?)', list(meta.items()))
    finally:
        conn.close()
    return path

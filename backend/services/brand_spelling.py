"""品牌「斜杠复合写法」的拆分判据，以及判不了时问模型一次的那一层。

为什么单独放一个模块：这两件事都**不放宽任何既有判据**，只回答同一个问题的两个
不同来源——「另一种写法是不是同一家」。既有判据（归一相等 / 企业登记名 / 别名表）
一个字没动，都在 :mod:`backend.services.brand_model_rules` 里。

## 1. 斜杠复合写法（可枚举的那一类）

实测（``回归基线`` Q9）：一条真成交
单价被误拒，理由写「引文中的品牌是 梅特勒托利多/Mettler-Toledo，与目标品牌
梅特勒-托利多 不一致」。根因不是别名不够——``identity()`` 本来就抹掉空格与连字符，
``梅特勒-托利多`` 与 ``梅特勒托利多`` 归一后相等——是**「中文/English」这种一格
两种写法**把品牌值拆散了：斜杠不在归一化的移除列表里，于是
``梅特勒托利多/mettler-toledo`` 与 ``梅特勒托利多`` 不等。

判据刻意窄，而且**只做逐段全等**：

* **恰好一侧含斜杠**才拆。两侧都含斜杠时不猜（``(卢)湘仪/湘鑫`` 这类"一格并列
  两个品牌"的写法留在原判据里被拒，是既有测试锁定的行为）；
* 只有**语言互补**的一对才走这条路：两侧都含拉丁字母、至少一侧含中日韩文字。
  实测的斜杠复合值都是"中文/English"这个形状；纯中文的写法不走（否则
  ``(卢)湘仪/湘鑫`` 对 ``湘鑫`` 会被误放行）；
* 拆出来的每一段必须**整段**与对面相等（或经别名表归一到同一个规范写法），
  不做任何子串/包含判断——``梅特勒`` 与 ``梅特勒托利多`` 仍然判成两家。

实测覆盖（口径：``16-final-run.json`` 台账 58 轮、56 条 ``brand_mismatch``，
其中含斜杠的品牌值 3 条）：``梅特勒托利多/Mettler-Toledo`` 2 条被救回，
``赛多利斯/sartorius`` 1 条在 ``sartorius`` 登记进 ``_BRAND_ALIASES`` 后救回。

## 2. 判不了的一对：在线问模型一次

写不完的是真别名（``赛多利斯`` ↔ ``Sartorius``、``一恒`` ↔ ``上海一恒`` 那类跨语言
对应）。既有立场是"拒了只是少一条价，收了可能是别家的价"，所以**没有依据就不登记**。
这里补的是"依据从哪来"：一对品牌写法，**形态上像同一家**（:func:`_may_be_same_brand`
的窄触发）而既有判据判不了时，在线问模型一次，把结论**连证据**落盘；同一条记录
下次直接命中，不再问第二遍。

* 默认**关**（``LABPILOT_BRAND_LEARN=1`` 才开）。不设 ``DEEPSEEK_API_KEY`` 时即使
  开了也学不到东西。
* 落盘位置是**进程临时目录**（``LABPILOT_BRAND_LEARN_DIR`` 可覆盖），不写仓库、
  不碰工作区共享的 ``output/``——多工位并行时不会互相写坏。
* 只把**同一位**（含证据）的结论登记进别名表；``不同一家`` 与 ``判不了`` 只落盘
  留痕，不进表（不登记就不会改变任何判定）。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

#: 一格写两个写法时用的分隔符。只认斜杠：``identity()`` 先做 NFKC，全角 ``／``
#: 到这里已经变成 ``/``，所以不用单独列。
_SEPARATOR = re.compile(r'/')

#: 中日韩文字。与 ``procurement_rules`` 里的同名字符类口径一致（含扩展 A 区与
#: 兼容表意文字），只用来判"这一段是不是中文品牌写法"。
_CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')

#: 一段里出现字母或数字混排的型号残渣（``BSC-1604``、``ME204E``）就不当品牌段。
_SEGMENT_MODEL_NOISE = re.compile(r'\d')

#: 明显不是品牌写法的词：抽出来的行首标签、表头、占位符。
_NON_BRAND_WORDS = {
    '无', '未知', '不详', '其他', '其它', '国产', '进口', '不限', '任意', '待定',
    '品牌', '厂家', '制造商', '生产商', '供应商', '合计', '小计', '型号', '规格',
    '序号', '备注', '单价', '数量', '单位', '金额', '名称', '略', '见上', '同上',
}

#: 企业登记名的形态后缀（与 ``brand_model_rules._ENTERPRISE_SUFFIX``
#: 同一份口径的粗筛版）。带这些后缀的一侧本身就是登记全名，是否同一家交给既有
#: 的"企业登记名"分支去判，不在这里问模型——问了也是同一条证据。
_ENTERPRISE_HINT = re.compile(
    r'(?:股份有限公司|有限责任公司|有限公司|仪器设备|科学仪器|仪器仪表|生物科技'
    r'|生物技术|医疗器械|实验仪器|实验设备|仪器公司|设备公司|商贸|贸易|科贸)'
)

#: 一次进程里最多问模型几次。超过就退回"判不了"，不再花额度；这一对也不落
#: "不同一家"（它只是没问成，不是问出了结论）。
_MAX_LEARNED_PER_PROCESS = 20

#: 判官模型返回的字段；提示词里逐字写给它看。
_JUDGE_SYSTEM_PROMPT = (
    '你在核对两份政府采购/中标公告里写的品牌名是不是同一家厂商。'
    '只按你知道的厂商事实回答，不要按字面像不像回答。'
    '拿不准就回答 false，并写明这一点。'
)

_LEARN_FILE_NAME = 'labpilot-brand-learned.json'
_LEARN_SCHEMA = 1

_learned_lock = RLock()
_learned_cache: dict | None = None
_learned_asked = 0
_memory_judgements: dict[tuple[str, str], dict] = {}
_injected_judge = None


# ---------------------------------------------------------------------------
# 1. 分词与准入闸
# ---------------------------------------------------------------------------

def segment_tokens(value: str) -> tuple[str, ...]:
    """把品牌值按斜杠切成归一化后的段；没有斜杠时返回空元组。

    归一化走 :func:`backend.services.brand_model_rules.identity`
    （NFKC + 去空格与连字符 + ``casefold``），所以 ``Mettler-Toledo`` 这一段
    在比对前已经变成 ``mettlertoledo``；全角 ``／`` 也在 NFKC 这一步变成 ``/``。
    """
    if not isinstance(value, str):
        return ()
    normalized = _normalize(value)
    if '/' not in normalized:
        return ()
    return tuple(part for part in _SEPARATOR.split(normalized) if part)


def _normalize(value: str) -> str:
    """与 ``identity()`` 同一条归一化口径；空值返回空串（这里不抛异常）。"""
    from backend.services.brand_model_rules import identity
    try:
        return identity(value)
    except (TypeError, ValueError):
        return ''


def _may_be_a_brand_word(token: str) -> bool:
    """这个词本身像不像"品牌写法"。

    排除：抽出来的表头/占位符、型号残渣（带数字）、明显是句子残渣的长串。
    """
    if len(token) < 2 or len(token) > 40:
        return False
    if token in _NON_BRAND_WORDS:
        return False
    if _SEGMENT_MODEL_NOISE.search(token):
        return False
    return bool(_CJK.search(token) or re.fullmatch(r'[a-z][a-z0-9]*', token))


def same_brand_by_segments(left: str, right: str, *, aliases) -> bool:
    """斜杠复合写法与单写法是不是同一家。**只做逐段全等**。

    ``aliases`` 传的是 ``identity()`` 后的写法到规范写法的表；判据本身不碰表
    以外的任何知识。

    两道闸，缺一不可：

    1. **语言互补**（:func:`_is_bilingual_pair`）：两侧都含拉丁字母、至少一侧
       含中日韩文字。实测的斜杠复合值都是"中文/English"或"中文/英文商标"这个
       形状；纯中文的 ``(卢)湘仪/湘鑫`` 因此整条不走这条路，留给原判据拒；
    2. **逐段全等**：复合侧拆开后某一段与另一侧**整段**相等，或经别名表落到
       同一个规范写法。不做子串/包含判断。
    """
    if not _is_bilingual_pair(left, right):
        return False
    left_parts = segment_tokens(left)
    right_parts = segment_tokens(right)
    if bool(left_parts) == bool(right_parts):     # 两侧都带斜杠 / 都不带：不猜
        return False
    compound, single = (left_parts, _normalize(right)) if left_parts \
        else (right_parts, _normalize(left))
    if not single:
        return False
    if any(len(part) < 2 for part in compound):
        # 单字母段不是品牌写法（``A/赛多利斯`` 这种一格并列别家的写法），不拆。
        return False
    for part in compound:
        if part == single:
            return True
        mapped = aliases.get(part)
        if mapped is not None and _canonical(mapped) == single:
            return True
    return False


def _is_bilingual_pair(left: str, right: str) -> bool:
    """这一对是不是"中文写法 ↔ 拉丁字母写法"的形状。

    * 两侧都要有拉丁字母——型号样的残渣（``(卢)湘仪`` 那种带括号的、或纯中文的
      并列写法）过不了这一关；
    * 至少一侧要有中日韩文字——两侧都是英文时，斜杠更可能是别的东西
      （``A/B`` 型号并列），不在这里猜。
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    both = _normalize(left) + _normalize(right)
    if not re.search(r'[a-z]', both):
        return False
    return bool(_CJK.search(left)) or bool(_CJK.search(right))


def _canonical(value) -> str:
    return _normalize(str(value)).replace('/', '')


# ---------------------------------------------------------------------------
# 2. 判不了的一对：问模型一次
# ---------------------------------------------------------------------------

def judge_enabled() -> bool:
    """学不学，看配置。默认关：不开就不会有任何网络调用。"""
    return os.environ.get('LABPILOT_BRAND_LEARN', '0').strip() == '1'


def learned_path() -> Path:
    override = os.environ.get('LABPILOT_BRAND_LEARN_DIR')
    base = Path(override) if override else Path(tempfile.gettempdir())
    return base / _LEARN_FILE_NAME


def load_learned() -> dict:
    """读落盘的判定台账；文件坏了当空表，并把这一条留给下一次覆盖写。

    进程内按 ``(mtime, size)`` 缓存：价格核验会在每一行上问"这一对判过没有"，
    每行都开一次文件不合适。文件被别的进程改写时缓存自动失效。
    """
    global _learned_cache
    path = learned_path()
    try:
        stat = path.stat()
    except OSError:
        _learned_cache = {'key': None, 'records': {}}
        return {}
    stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    with _learned_lock:
        if _learned_cache is not None and _learned_cache.get('key') == stamp:
            return _learned_cache['records']
    records = _read_learned(path)
    with _learned_lock:
        _learned_cache = {'key': stamp, 'records': records}
    return records


def _read_learned(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get('schema_version') != _LEARN_SCHEMA:
        return {}
    records = payload.get('judgements')
    return records if isinstance(records, dict) else {}


def _pair_key(left: str, right: str) -> str:
    """一条判定对应一对写法；两边互换是同一条，只问一次。"""
    first, second = sorted((_canonical(left), _canonical(right)))
    return f'{first}\t{second}'


def lookup(left: str, right: str) -> dict | None:
    """这一对问过没有。返回 ``{'same': bool, 'canonical': str|None, ...}`` 或 ``None``。"""
    key = _pair_key(left, right)
    in_memory = _memory_judgements.get(key)
    if in_memory is not None:
        return in_memory
    record = load_learned().get(key)
    return record if isinstance(record, dict) else None


def ask_model(left: str, right: str) -> dict | None:
    """在线问模型一次，返回判定；问不成返回 ``None``（不落"不同一家"）。"""
    global _learned_asked
    judge = _injected_judge
    if judge is None:
        judge = _build_default_judge()
        if judge is None:                  # 没配凭据：不产生任何调用
            return None
    with _learned_lock:
        if _learned_asked >= _MAX_LEARNED_PER_PROCESS:
            return None
        _learned_asked += 1                # 只数真的走出去的调用
    try:
        answer = judge(left, right)
    except Exception:                      # 网络/额度/格式任何问题：退回"判不了"
        return None
    if not isinstance(answer, dict) or not isinstance(answer.get('same'), bool):
        return None
    record = {
        'same': answer['same'],
        'canonical': answer.get('canonical') if isinstance(answer.get('canonical'), str) else None,
        'basis': str(answer.get('basis') or ''),
        'evidence': [str(url) for url in (answer.get('evidence') or []) if str(url).strip()],
        'source': str(answer.get('source') or 'model'),
        'asked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'left': left,
        'right': right,
    }
    _remember(left, right, record)
    return record


def _remember(left: str, right: str, record: dict) -> None:
    key = _pair_key(left, right)
    _memory_judgements[key] = record
    with _learned_lock:
        store = load_learned()
        store[key] = record
        _write_learned(store)


def _write_learned(store: dict) -> None:
    path = learned_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_suffix('.tmp')
        scratch.write_text(
            json.dumps({'schema_version': _LEARN_SCHEMA, 'judgements': store},
                       ensure_ascii=False, indent=2, sort_keys=True),
            encoding='utf-8')
        os.replace(scratch, path)          # 并发写工位也不会读到半截文件
    except OSError:
        return                             # 落不了盘就只在内存里生效，判定不受影响


def reject_pair(left: str, right: str) -> None:
    """记一条"不同一家"（模型说的）。落盘只为不再问第二遍，**不进别名表**。"""
    _remember(left, right, {
        'same': False, 'canonical': None, 'basis': '模型判定：不是同一家',
        'evidence': [], 'source': 'model',
        'asked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'left': left, 'right': right,
    })


def _build_default_judge():
    """有凭据才给默认判官；没配 ``DEEPSEEK_API_KEY`` 就是"判不了"，不抛异常。"""
    try:
        from backend.infrastructure.deepseek import DeepSeekClient
        client = DeepSeekClient.from_env()
    except Exception:
        return None
    return lambda left, right: _deepseek_judge(client, left, right)


def _deepseek_judge(client, left: str, right: str) -> dict | None:
    """默认判官：项目自己的 :class:`DeepSeekClient`，零依赖注入。"""
    payload = {
        '品牌写法 A': left,
        '品牌写法 B': right,
        '要求': {
            'same': '两家是不是同一家厂商（true/false）',
            'canonical': '同一家时，这个品牌最通用的一个写法；不同一家时给 null',
            'basis': '一句话说明依据（例如"METTLER TOLEDO 的中文名是梅特勒-托利多"）',
            'evidence': '可核实的来源链接数组，至少一条；没有就给空数组',
        },
    }
    answer = client.analyze(
        system_prompt=_JUDGE_SYSTEM_PROMPT,
        user_prompt=json.dumps(payload, ensure_ascii=False),
    )
    if not isinstance(answer, dict):
        return None
    canonical = answer.get('canonical')
    if canonical is not None and not isinstance(canonical, str):
        canonical = None
    evidence = answer.get('evidence')
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list):
        evidence = []
    return {
        'same': answer.get('same') is True,
        'canonical': canonical,
        'basis': str(answer.get('basis') or ''),
        'evidence': evidence,
        'source': 'deepseek',
    }


def set_judge(judge) -> None:
    """测试/离线重放注入判官；传 ``None`` 恢复默认。

    这也是"离线重放不许联网"的开关：重放脚本注入一个固定判官，或直接不开
    :func:`judge_enabled`。
    """
    global _injected_judge
    _injected_judge = judge


def _reset_for_tests() -> None:
    """清进程内状态（含"这一对问过了"与读盘缓存），不动落盘文件。"""
    global _learned_asked, _injected_judge, _learned_cache
    with _learned_lock:
        _memory_judgements.clear()
        _learned_asked = 0
        _injected_judge = None
        _learned_cache = None


# ---------------------------------------------------------------------------
# 3. 该不该问：窄触发
# ---------------------------------------------------------------------------

def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r'[a-z0-9]+', value) if token}


def may_be_same_brand(left: str, right: str) -> bool:
    """形态上"像同一家"吗——只有像，才值得花一次模型调用。

    三条同时成立：

    1. 两侧都是像品牌写法的词：不是表头/占位符、不带数字（型号残渣）、不带企业
       形态后缀（那是登记全名，交给既有的登记名分支）；
    2. 至少一侧是中文或拉丁字母的品牌写法；
    3. **有一点形态关联**：一侧中文、另一侧纯拉丁字母（跨语言对照，这一类的
       知识只有模型有——``艾本德`` 与 ``Eppendorf``、``赛多利斯`` 与 ``Sartorius``），
       或者有更具体的关联——拆开后的某一段与对面整段相等、共用一个 ASCII 词
       （``sartorius`` 与 ``Sartorius``）、一侧的中文串含在另一侧里
       （``梅特勒`` 与 ``梅特勒托利多``）。

    没有关联就根本不像同一家（``苏净安泰`` 与 ``江苏富泰`` 一个词都不共用），
    问了是白花额度。
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    first, second = segment_tokens(left), segment_tokens(right)
    flat = (first or (_canonical(left),), second or (_canonical(right),))
    for side in flat:
        for token in side:
            if not _may_be_a_brand_word(token):
                return False
            if _ENTERPRISE_HINT.search(token):
                return False
    if _canonical(left) == _canonical(right):
        return False                           # 归一就相等：根本不用问
    # 一侧中文、另一侧纯拉丁字母：跨语言对照，只有"是不是同一家"这件事需要知识
    left_cjk_only = bool(_CJK.search(left)) and not _CJK.search(right)
    right_cjk_only = bool(_CJK.search(right)) and not _CJK.search(left)
    if left_cjk_only or right_cjk_only:
        return bool(re.search(r'[a-z]', _canonical(left) + _canonical(right)))
    # 一段与对面整段相等（``X/Y`` 对 ``Y``）
    for side, other in ((first, second), (second, first)):
        if side and other:
            for token in side:
                if token in other:
                    return True
                if token in (_canonical(left), _canonical(right)):
                    return True
    # 共用一个 ASCII 词
    left_tokens, right_tokens = _tokens(_canonical(left)), _tokens(_canonical(right))
    if left_tokens & right_tokens:
        return True
    # 一侧的中文串含在另一侧里
    left_cjk, right_cjk = ''.join(_CJK.findall(_canonical(left))), ''.join(_CJK.findall(_canonical(right)))
    if left_cjk and right_cjk and (left_cjk in right_cjk or right_cjk in left_cjk):
        return True
    return False


__all__ = [
    'segment_tokens',
    'same_brand_by_segments',
    'may_be_same_brand',
    'judge_enabled',
    'learned_path',
    'load_learned',
    'lookup',
    'ask_model',
    'reject_pair',
    'set_judge',
    '_reset_for_tests',
]

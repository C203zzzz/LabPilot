/**
 * 「配置所属型号」在候选卡片上的展示口径（设计规范）。
 *
 * 设计规范：**抽到的配置属于哪个型号就展示哪个型号**。起因是洁净台那一轮
 * （``b3-opt-cleanbench-boxun-bj1cd``）：候选 ``bekomat30`` 的链接指向
 * ``ProductDetail/634.html``（那页是 ``BBS-H1300``），而它的配置抽自
 * ``ProductDetail/630.html``（那页是 ``BBS-V600``，配置第一行就写着），
 * ``bekomat30`` 两个页面上都不存在（``设计规范`` §1.1）。
 *
 * 后端 迭代规范 已经把这一格算好并写在候选上（``candidate.configuration_model`` 与
 * ``configuration_model_basis``，判不出时空串）。这一层**只决定要不要显示、怎么显示**，
 * 一个字节都不改：
 *
 * - 空串 → 不显示（后端判不出就是不猜，界面上也不许拿候选自己的型号顶上）；
 * - 与候选自己的 ``model`` 归一后相同 → 不显示（同一件事不重复说）；
 * - 归一后不同 → 显示「配置所属型号：<认出的型号>（原候选写法：<候选自己的型号>）」，
 *   **两个都留着**：候选型号是"查这个的价格"的键（``candidate_id`` 就是按它算的），
 *   也是这条线索的来源痕迹，不能被抹掉。
 *
 * 归一用的是页面上配对型号那同一条尺子（``normalizeModel``：全角、大小写、空白与
 * 连字符不影响判定），不新造第二套口径。
 */
import { normalizeModel } from './rejectionPlacement.js'

/**
 * 这条候选要不要在卡片上说出"配置所属型号"。
 *
 * 返回 ``null``（不显示）或 ``{model, original}``（``original`` 是候选自己的型号写法，
 * 为空时渲染层只印认出的那一个）。
 */
export function configurationModelNotice(candidate) {
  const model = String((candidate || {}).configuration_model || '').trim()
  if (!model) return null
  const original = String((candidate || {}).model || '').trim()
  if (original && normalizeModel(model) === normalizeModel(original)) return null
  return { model, original }
}

/** 卡片上那一行的逐字文案（渲染与测试共用一处，避免两处措辞漂移）。 */
export function configurationModelLine(notice) {
  if (!notice) return ''
  return notice.original
    ? `配置所属型号：${notice.model}（原候选写法：${notice.original}）`
    : `配置所属型号：${notice.model}`
}

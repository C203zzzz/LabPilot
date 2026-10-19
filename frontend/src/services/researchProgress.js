/**
 * Q11「进展到哪」用的纯数据层：只读后端已经写下来的信号，不编百分比。
 *
 * 字段来源（`GET /api/v1/tasks/{id}/research-runs/{run_id}` 的返回体，与
 * `backend/services/procurement_research.py` 的写入处一一对应）：
 *   - run.stage              当前阶段中文名（record_stage 写）
 *   - run.stage_timings      [{'name','stage','round','status','duration_ms','calls'}]，
 *                            阶段结束时追加一条（procurement_research.py:911 record_stage，
 *                            只保留最后 STAGE_TIMING_LIMIT 条）
 *   - run.lanes              {'price','alternatives'} → pending/running/completed/failed
 *   - run.usage / run.limits {'search','fetch','model'} 本轮已用与额度
 *                            （:869 用 usage + RESEARCH_LIMITS 得 limits）
 *   - run.completed_queries  已完成的检索式
 *   - run.round              当前轮次（supplemental_research.py:242 会跨轮合并 stage_timings）
 *
 * 为什么没有百分比：后端没有「本轮一共几个阶段」这种分母——record_stage 是追加，
 * 不是给一份固定清单打勾，阶段数随证据情况增加。拿它当分母就是编数字。
 * 页面用两个真实信号表达进展：阶段清单（只有已走完的阶段，当前阶段由状态行给出）
 * 和两条检索线的完成条数；进度条读的是「检索线完成数 / 检索线总数」，不是检索进度百分比。
 */
export const LANE_NAMES = [['price', '成交项目检索'], ['alternatives', '其他品牌比较']]
export const LANE_STATES = { pending: '尚未开始', running: '正在检索', completed: '已结束', failed: '已中断' }
export const CALL_NAMES = [['search', '检索'], ['fetch', '读取页面'], ['model', '模型调用']]
export const PROGRESS_STATUS_LABELS = { ready: '采购条件已确认', queued: '等待开始', running: '正在研究', partial: '部分完成', completed: '已完成', cancelled: '已停止', interrupted: '已中断', failed: '执行未完成' }

export function progressFacts(run) {
  const lanes = run?.lanes || {}
  const done = LANE_NAMES.filter(([key]) => lanes[key] === 'completed').length
  const stages = (run?.stage_timings || []).filter(entry => (entry.round || 1) === (run?.round || 1))
  const shown = stages.slice(-6)
  const usage = run?.usage || {}
  const limits = run?.limits || {}
  return {
    done,
    total: LANE_NAMES.length,
    earlierCount: Math.max(0, stages.length - shown.length),
    shown,
    currentStage: run?.stage || '',
    lanes: LANE_NAMES.map(([key, name]) => ({ key, name, state: lanes[key] || '', text: LANE_STATES[lanes[key]] || '状态待确认' })),
    counts: CALL_NAMES.map(([key, name]) => ({ key, name, used: usage[key] || 0, limit: limits[key] || 0 })),
    queries: (run?.completed_queries || []).length,
  }
}

/**
 * How many pages were read locally, how many by paid vision, how many remain.
 *
 * Why this is its own module
 * --------------------------
 * The previous summary produced three numbers that were neither exclusive nor
 * exhaustive, so they did not add up to the page count and two pages vanished
 * from the interface entirely. On a real 17-page upload it showed
 * "本地文字 7 页 · 视觉识别 8 页 · 待处理 9 页": 7+8=15 (two pages in neither
 * bucket) while the 9 "待处理" overlapped all 7 local pages and 2 of the paid
 * ones. A reader could not tell which pages were actually unread.
 *
 * The three displayed numbers are now a strict partition of the page list:
 *
 *     视觉识别 + 本地文字 + 待识别 = 总页数
 *
 * `deferred_by_page_cap` is reported separately as a *reason* (why a page was
 * not paid for), never as a fourth category — a page is deferred *and* either
 * read locally or still pending.
 */

/** A page counts as paid vision only when a vision analysis actually completed. */
const readByVision = page =>
  (page.provenance || []).some(
    item => item.kind === 'visual_page_analysis' && item.status === 'completed')

/** Local text: the pipeline decided no remote reading was needed. */
const readLocally = page => page.route === 'native_text'

/** Not yet read at all: no completed vision and no local text. */
const stillPending = page => !readByVision(page) && !readLocally(page)

/** Pages held back by the page cap; a reason, not a reading outcome. */
const deferredByCap = page => page.route_reason === 'deferred_by_page_cap'

/**
 * Split pages into the three mutually exclusive reading outcomes.
 *
 * @returns {{visual: number, local: number, pending: number, deferred: number, total: number}}
 */
export function pageProgress(pages) {
  const list = pages || []
  const visual = list.filter(readByVision).length
  const local = list.filter(page => !readByVision(page) && readLocally(page)).length
  const pending = list.filter(stillPending).length
  return {
    visual,
    local,
    pending,
    deferred: list.filter(deferredByCap).length,
    total: list.length,
  }
}

/**
 * One-line summary for the document row.
 *
 * The counts always reconcile to the total, so the user can trust that no page
 * was silently dropped.
 */
export function pageRouteSummary(pages) {
  const { visual, local, pending, deferred, total } = pageProgress(pages)
  if (!total) return '尚未识别'
  const parts = []
  if (local) parts.push(`本地文字 ${local} 页`)
  if (visual) parts.push(`视觉识别 ${visual} 页`)
  // Always name the remainder, even when it is zero, so the three numbers are
  // visibly a partition rather than a partial report.
  parts.push(`待识别 ${pending} 页`)
  const detail = deferred ? `；其中 ${deferred} 页因识别页数上限暂缓` : ''
  return `${parts.join(' · ')}（共 ${total} 页${detail}）`
}

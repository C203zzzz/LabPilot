/**
 * 报告版本对比在页面上的排版口径。
 *
 * 后端 ``GET /reports/{id}/diff`` 给的是"哪一条变了、怎么变的"，这里只决定**怎么摆**：
 *
 * - 价格、配置、缺口三类**永远都在**——某一类恰好没变时也要让读者看见"这一类没变"，
 *   而不是让这一类从页面上消失（消失了就分不清"没变"和"没比"）；
 * - 一条差异画几行由 ``changeLines`` 决定：added 不印"上一版：（空）"，
 *   removed 要写清"这一条本版不再出现"，读者才不会把新增读成"原来是空的"；
 * - 没变的条目数用后端给的数字（``unchanged.items``）报出来，前端不自己数一遍——
 *   同一件事两个出口迟早会说成两个数。
 */

export function diffGroups(payload) {
  return ((payload && payload.categories) || []).map(category => {
    const items = category.items || []
    return { key: category.key, title: category.title || '', items, empty: !items.length }
  })
}

export function changeLines(change) {
  const lines = []
  if (change.change !== 'added') lines.push({ tone: 'before', text: `上一版：${change.before || '（空）'}` })
  if (change.change !== 'removed') {
    lines.push({ tone: 'after', text: `${change.change === 'added' ? '本版新增' : '本版'}：${change.after || '（空）'}` })
  }
  if (change.change === 'removed') lines.push({ tone: 'removed', text: '这一条本版不再出现' })
  return lines
}

export function unchangedSummary(payload) {
  const count = (payload && payload.unchanged && payload.unchanged.items) || 0
  return `其余 ${count} 条条目两版逐字相同。`
}

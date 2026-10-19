// 历史报告的批量删除规则。
//
// 界面上勾的是"报告"，删掉的却是报告所属的整项任务（连同名下全部报告、原始
// 资料和价格记录）。选中集里因此可能挤着同一任务的许多份报告，也可能混进
// 拿不到 ``task_id`` 的行，所以这里用纯函数把"勾了几份报告"换算成"会删几个
// 任务"，并且只使用行里真实存在的字段：拿不到就明说，不猜、不静默跳过。

export const REPORT_BATCH_LIMIT = 200

// 正在研究中的任务状态。口径照旧：旧的任务列表用 queued/running 判断，后端
// 删任务时会一并清掉它的分析租约，执行侧在检查点读到租约丢失就退出——也就是
// 那次研究被中断。这是删除前的安全信息，确认框必须在动手前说出来。
const RUNNING_PROCUREMENT_STATES = new Set(['queued', 'running'])

// 选中的任务里有几个正在研究。tasks 拿不到（没加载或不是数组）时返回 0：
// 宁可不写这句，也不编一个数字出来。
export function runningTaskCount(taskIds, tasks) {
  const ids = new Set(Array.isArray(taskIds) ? taskIds : [])
  if (!ids.size || !Array.isArray(tasks)) return 0
  return tasks.filter(item => item && ids.has(item.id) && RUNNING_PROCUREMENT_STATES.has(item.procurement_status)).length
}

// 去重后的任务 id，按选中报告首次出现的顺序；缺 task_id 的行单独交出来。
export function selectedReportTasks(reports) {
  const list = Array.isArray(reports) ? reports.filter(Boolean) : []
  const taskIds = []
  const reportsWithoutTask = []
  for (const item of list) {
    const taskId = typeof item?.task_id === 'string' ? item.task_id.trim() : ''
    if (!taskId) {
      reportsWithoutTask.push(item)
      continue
    }
    if (!taskIds.includes(taskId)) taskIds.push(taskId)
  }
  return { taskIds, reportsWithoutTask }
}

// 确认框要回答的是"点了确认会发生什么"，所以数字分三档：勾了多少份报告、
// 会删多少个任务、这些任务名下还有多少份没勾的报告会一起消失；如果其中有任务
// 正在研究，还要在动手前说清那次研究会被中断。
export function reportTaskDeletion(selectedReports, libraryReports = [], tasks = []) {
  const list = Array.isArray(selectedReports) ? selectedReports.filter(Boolean) : []
  const { taskIds, reportsWithoutTask } = selectedReportTasks(list)
  const reportCount = list.filter(item => taskIds.includes(item?.task_id)).length
  const owned = (Array.isArray(libraryReports) ? libraryReports : [])
    .filter(item => item && taskIds.includes(item.task_id))
  const ownedCount = owned.length
  const extraCount = Math.max(0, ownedCount - reportCount)
  const untraceableCount = reportsWithoutTask.length
  const taskCount = taskIds.length
  const runningCount = runningTaskCount(taskIds, tasks)

  const sentences = []
  if (taskCount) {
    sentences.push(`选中的 ${reportCount} 份报告属于 ${taskCount} 个任务；将删除这 ${taskCount} 个任务，连同它们名下的全部报告、原始资料和价格记录。`)
    if (extraCount) sentences.push(`这 ${taskCount} 个任务在报告列表里共 ${ownedCount} 份报告，没勾选的 ${extraCount} 份也会一起删除。`)
  }
  if (untraceableCount) sentences.push(`另有 ${untraceableCount} 份报告没有记录所属任务，本次不会删除。`)
  if (runningCount) sentences.push(`其中 ${runningCount} 个任务仍在研究进行中，删除会中断它们。`)

  return {
    taskIds,
    reportCount,
    taskCount,
    ownedCount,
    extraCount,
    untraceableCount,
    runningCount,
    canDelete: taskCount > 0,
    title: taskCount ? `删除选中的 ${reportCount} 份报告所属的 ${taskCount} 个任务？` : '这些报告没有记录所属任务',
    detail: taskCount
      ? `${sentences.join('')}删除后无法恢复。`
      : '列表里没有它们的所属任务，无法按任务删除。请刷新报告列表后重试。',
  }
}

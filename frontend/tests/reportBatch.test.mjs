import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

import {REPORT_BATCH_LIMIT, reportTaskDeletion, runningTaskCount, selectedReportTasks} from '../src/services/reportBatch.js'

const library = [
  {id: 'r-a1', task_id: 'task-a', title: '酶标仪选型', version: 1},
  {id: 'r-a2', task_id: 'task-a', title: '酶标仪选型', version: 2},
  {id: 'r-a3', task_id: 'task-a', title: '酶标仪选型', version: 3},
  {id: 'r-b1', task_id: 'task-b', title: '离心机选型', version: 1},
]

test('one task id per selected report, in the order the reports were picked', () => {
  assert.deepEqual(selectedReportTasks([library[0], library[1], library[3]]).taskIds, ['task-a', 'task-b'])
  assert.deepEqual(selectedReportTasks([]).taskIds, [])
  assert.deepEqual(selectedReportTasks(undefined).taskIds, [])
  assert.deepEqual(selectedReportTasks([null, library[0]]).taskIds, ['task-a'])
  assert.deepEqual(selectedReportTasks([{id: 'r-x'}, {id: 'r-y', task_id: '   '}]).taskIds, [])
})

test('reports without a task id are handed back instead of silently skipped', () => {
  const {taskIds, reportsWithoutTask} = selectedReportTasks([library[0], {id: 'r-x', title: '来路不明的报告'}])
  assert.deepEqual(taskIds, ['task-a'])
  assert.deepEqual(reportsWithoutTask.map(item => item.id), ['r-x'])
})

test('the confirmation counts reports, tasks and the reports that come along', () => {
  const plan = reportTaskDeletion([library[0], library[3]], library)
  assert.equal(plan.reportCount, 2)
  assert.equal(plan.taskCount, 2)
  assert.equal(plan.ownedCount, 4)
  assert.equal(plan.extraCount, 2)
  assert.equal(plan.canDelete, true)
  assert.equal(plan.title, '删除选中的 2 份报告所属的 2 个任务？')
  assert.match(plan.detail, /选中的 2 份报告属于 2 个任务；将删除这 2 个任务，连同它们名下的全部报告、原始资料和价格记录/)
  assert.match(plan.detail, /这 2 个任务在报告列表里共 4 份报告，没勾选的 2 份也会一起删除/)
  assert.match(plan.detail, /删除后无法恢复/)
})

test('picking three reports of one task still says only one task dies', () => {
  const plan = reportTaskDeletion([library[0], library[1], library[2]], library)
  assert.equal(plan.reportCount, 3)
  assert.equal(plan.taskCount, 1)
  assert.equal(plan.extraCount, 0)
  assert.equal(plan.title, '删除选中的 3 份报告所属的 1 个任务？')
  assert.ok(!plan.detail.includes('没勾选的'), '没有额外报告时不该提"还有多少份一起删"')
})

test('an untraceable report is named and left out of the request', () => {
  const plan = reportTaskDeletion([library[0], {id: 'r-x'}], library)
  assert.deepEqual(plan.taskIds, ['task-a'])
  assert.equal(plan.untraceableCount, 1)
  assert.match(plan.detail, /另有 1 份报告没有记录所属任务，本次不会删除/)
})

test('a selection nobody can trace is never turned into a fake deletion', () => {
  const plan = reportTaskDeletion([{id: 'r-x'}, {id: 'r-y'}], library)
  assert.equal(plan.canDelete, false)
  assert.equal(plan.taskCount, 0)
  assert.deepEqual(plan.taskIds, [])
  for (const word of ['undefined', 'NaN', 'null', '0 个任务']) {
    assert.ok(!plan.title.includes(word) && !plan.detail.includes(word), `确认文案里不该出现「${word}」`)
  }
  const empty = reportTaskDeletion([], library)
  assert.equal(empty.canDelete, false)
  assert.equal(empty.reportCount, 0)
  assert.equal(empty.taskCount, 0)
})

test('the batch cap is still 200 reports per round', () => {
  assert.equal(REPORT_BATCH_LIMIT, 200)
})

test('a task that is still being researched is called out before deleting', () => {
  const plan = reportTaskDeletion([library[0], library[3]], library, [
    {id: 'task-a', procurement_status: 'running'},
    {id: 'task-b', procurement_status: 'completed'},
  ])
  assert.equal(plan.runningCount, 1)
  assert.match(plan.detail, /其中 1 个任务仍在研究进行中，删除会中断它们/)
  // 排队中的也算「仍在研究」
  assert.equal(runningTaskCount(['task-a'], [{id: 'task-a', procurement_status: 'queued'}]), 1)
  // 只统计选中任务里的：别的任务在跑不算
  const unrelated = reportTaskDeletion([library[0]], library, [{id: 'task-b', procurement_status: 'running'}])
  assert.equal(unrelated.runningCount, 0)
  assert.ok(!unrelated.detail.includes('仍在研究进行中'))
  // 两个选中任务都在跑就说两个
  assert.equal(reportTaskDeletion([library[0], library[3]], library, [
    {id: 'task-a', procurement_status: 'running'},
    {id: 'task-b', procurement_status: 'queued'},
  ]).runningCount, 2)
})

test('no research running means the sentence never appears', () => {
  const idle = [{id: 'task-a', procurement_status: 'completed'}, {id: 'task-b', procurement_status: 'draft'}]
  for (const plan of [
    reportTaskDeletion([library[0], library[3]], library, idle),
    reportTaskDeletion([library[0], library[3]], library, []),
    reportTaskDeletion([library[0], library[3]], library, undefined),
    reportTaskDeletion([library[0], library[3]], library, [{id: 'task-a'}]),
    reportTaskDeletion([library[0], library[3]], library),
  ]) {
    assert.equal(plan.runningCount, 0)
    assert.ok(!plan.detail.includes('仍在研究进行中'), '没有正在研究的任务时一个字都不该多')
  }
  // tasks 拿不到时不许编数字
  assert.equal(runningTaskCount(['task-a'], undefined), 0)
  assert.equal(runningTaskCount(undefined, idle), 0)
})

// --- 结构回归：报告列表真的删任务、真的能打开任务首页 ---------------------------

const page = readFileSync(new URL('../src/pages/ReportLibrary.jsx', import.meta.url), 'utf8')
const workspace = readFileSync(new URL('../src/pages/WorkspacePages.jsx', import.meta.url), 'utf8')
const api = readFileSync(new URL('../src/services/api.js', import.meta.url), 'utf8')

test('deleting from the report list deletes the tasks, not the reports', () => {
  assert.match(page, /taskApi\.deleteTasks\(taskIds\)/)
  assert.ok(!page.includes('taskApi.deleteReports'), '报告列表不该再走只删报告的 /reports/delete')
  assert.match(page, /await onTasksDeleted\?\.\(taskIds\)/)
  assert.match(page, /className="report-delete-confirm" role="alert"/)
  assert.match(page, /\{deletion\.title\}/)
  assert.match(page, /\{deletion\.detail\}/)
  assert.match(page, /确认删除 \$\{deletion\.taskCount\} 个任务/)
})

test('every row opens the task home, and the report shortcut stays put', () => {
  assert.match(page, /navigate\(`\/app\/task\/\$\{item\.task_id\}`\)/)
  assert.match(page, /navigate\(`\/app\/task\/\$\{item\.task_id\}\?tab=\$\{encodeURIComponent\('报告'\)\}&report=\$\{encodeURIComponent\(item\.id\)\}`\)/)
  assert.match(page, /打开任务/)
  assert.match(page, /查看报告/)
})

test('the confirmation knows which selected tasks are still running', () => {
  assert.match(page, /export default function ReportLibrary\(\{ onTasksDeleted, tasks = \[\] \}\)/)
  assert.match(page, /reportTaskDeletion\(selectedItems, items, tasks\)/)
  assert.match(workspace, /<ReportLibrary tasks=\{tasks\}/)
})

test('the task record block is gone from the history page', () => {
  assert.ok(!workspace.includes('<h2>任务记录</h2>'), '历史页不该再有「任务记录」块')
  assert.ok(!workspace.includes('已保存的任务和报告会保留在这里'))
  assert.ok(!workspace.includes('history-task-list'))
  assert.ok(!workspace.includes('history-list-section'))
  assert.ok(!workspace.includes('tasks/taskBatch'))
  assert.match(workspace, /<PageHeading title="历史报告" \/>/)
})

test('the task batch endpoints the page still needs stay wired', () => {
  assert.match(api, /deleteTasks: taskIds => jsonRequest\('\/tasks\/delete'/)
  assert.match(api, /body: JSON\.stringify\(\{ task_ids: taskIds \}\)/)
})

test('deleting a task also refreshes the sidebar task list', () => {
  assert.match(page, /window\.dispatchEvent\(new Event\('labpilot-tasks-change'\)\)/)
  const shell = readFileSync(new URL('../src/app/AppShell.jsx', import.meta.url), 'utf8')
  assert.match(shell, /window\.addEventListener\('labpilot-tasks-change', load\)/)
  assert.match(shell, /window\.removeEventListener\('labpilot-tasks-change', load\)/)
})

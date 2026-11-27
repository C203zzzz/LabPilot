/**
 * 前端与导出报告共用同一批"内部话 → 人话"用例。
 *
 * 背景（盲测四份报告共有的缺陷）：异常类名（``PageReadError``、``TimeoutError``）、
 * 聚合站代号（``120shop``、``bidcenter``）、以及一句「（这是检索阶段的判定；研究阶段
 * 已取得正文并据其核价，见来源清单）」在**导出报告**里漏给读者，同一句话在页面上却是
 * 人话——页面上一直有这层翻译（``services/publicText.js``），导出那条路径没接上。
 *
 * 跨语言没法共用同一份代码，所以共用**同一份用例文件**：
 * ``tests/fixtures/internal_text_cases.json``（仓库根的 tests/fixtures，前端从
 * ``frontend/tests`` 往上两级读到）。后端 ``report_library.humanize_gap_text`` 对着
 * 同一份文件跑 ``tests/test_report_claims_and_frontend.py`` 里的对应用例。
 * 任何一端漏了新类别，自己这端的测试先红——这就是防分叉的机制。
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import test from 'node:test'

import { publicText } from '../src/services/publicText.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const casesPath = path.resolve(here, '..', '..', 'tests', 'fixtures', 'internal_text_cases.json')
const fixture = JSON.parse(readFileSync(casesPath, 'utf8'))
const EMPTY_STATE = { candidates: [], documents: [] }

test('内部话用例文件本身可读，且两类用例都不为空', () => {
  assert.ok(fixture.clean.length >= 10 && fixture.keep.length >= 2,
    '用例文件被改小了，防分叉就形同虚设')
  assert.ok(Object.keys(fixture.patterns).length >= 5)
})

test('页面文本里不许剩下任何内部符号（与后端同一批用例）', () => {
  const failures = []
  for (const item of fixture.clean) {
    const shown = publicText(item.text, EMPTY_STATE)
    if (shown !== item.expect) failures.push(`原文改了形状：${item.text}\n  期望 ${item.expect}\n  实际 ${shown}`)
    for (const [name, pattern] of Object.entries(fixture.patterns)) {
      if (new RegExp(pattern).test(shown)) failures.push(`${name} 漏出：${item.text} -> ${shown}`)
    }
  }
  assert.deepEqual(failures, [])
})

test('已经是人话、或来源自己的话，一个字都不许动', () => {
  const failures = []
  for (const item of fixture.keep) {
    const shown = publicText(item.text, EMPTY_STATE)
    if (shown !== item.text) failures.push(`${item.why}：${item.text} -> ${shown}`)
  }
  assert.deepEqual(failures, [])
})

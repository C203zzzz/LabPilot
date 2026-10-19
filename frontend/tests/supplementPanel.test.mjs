/**
 * 补证面板「补充缺少的证据」那一列缺口的渲染契约（SupplementPanel.jsx:62）。
 *
 * 那一列的数据来自 GET /research-runs/{id}/supplement 的 `gate.gaps[].missing[]`：
 *   - 字符串那一路是「来源读取失败原因」原文，后端 supplemental_research.py:40 把
 *     `source['reason']` 直接放进来，形如
 *     「该来源连续两次无法读取（empty_body），其他来源可正常访问；保留URL及原始失败原因待补查」。
 *     全量实测 376 条字符串里 28 条带内部代号（empty_body 11 / RemoteDisconnected 9 /
 *     dns_not_found 4 / tls_* 4）。
 *   - 对象那一路是参数名（{name, value, unit, direction, conditions}），共 924 条，0 条带代号。
 *
 * 修法与 error_reason（ResearchResults.jsx:291）一致：用 publicText 做句内替换，
 * 只换括号里的代号。这一列是「、」连接的短条目，塞整句话术会读不通。
 */
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

import {publicText} from '../src/services/publicText.js'

const SUFFIX = '，其他来源可正常访问；保留URL及原始失败原因待补查'
const reason = code => `该来源连续两次无法读取（${code}）${SUFFIX}`
const CODES = ['empty_body', 'empty_shell', 'dns_not_found', 'RemoteDisconnected', 'tls_BAD_ECPOINT']

const panel = readFileSync(new URL('../src/pages/SupplementPanel.jsx', import.meta.url), 'utf8')

test('缺口的字符串条目必须过翻译层，对象条目保持原样', () => {
  assert.match(panel, /import \{ publicText \} from '\.\.\/services\/publicText'/)
  assert.match(panel, /typeof m === 'string' \? publicText\(m, \{\}\) : m\.name/)
  assert.ok(!/typeof m === 'string' \? m : m\.name/.test(panel), '字符串条目又变回原样渲染了')
})

test('gate.reason 仍然走整句话术，没有被这次改动带偏', () => {
  assert.match(panel, /researchIssue\(gate\.reason\)\.message/)
})

test('这 28 条那一路：代号换掉，句子其余部分一字不动', () => {
  for (const code of CODES) {
    const shown = publicText(reason(code), {})
    assert.ok(!/[A-Za-z]+_[A-Za-z_]+/.test(shown), shown)
    assert.ok(!shown.includes('RemoteDisconnected'), shown)
    assert.ok(shown.endsWith(SUFFIX), '句子的有用部分被改掉了：' + shown)
  }
  assert.equal(publicText(reason('empty_body'), {}), `该来源连续两次无法读取（来源正文为空）${SUFFIX}`)
  assert.equal(publicText(reason('RemoteDisconnected'), {}), `该来源连续两次无法读取（连接被对方中断）${SUFFIX}`)
})

test('按组件那样拼成「、」列表之后，整条不再含内部代号', () => {
  // 组件是 `g.missing.map(...).join('、')`，同一个分组里还可能跟着一句普通中文缺口。
  const group = { model: '替代产品', missing: [...CODES.map(reason), '请寻找可核验的替代原始来源'] }
  const line = `${group.model || '替代产品'}：${group.missing.map(m => typeof m === 'string' ? publicText(m, {}) : m.name).join('、')}`
  assert.ok(!/[A-Za-z]+_[A-Za-z_]+/.test(line), line)
  assert.ok(!line.includes('RemoteDisconnected'), line)
  assert.ok(line.startsWith('替代产品：该来源连续两次无法读取（来源正文为空）'), line)
  assert.ok(line.includes('请寻找可核验的替代原始来源'), line)
  assert.ok(line.includes('保留URL及原始失败原因待补查'), line)
})

test('参数对象那一路不受影响，普通中文缺口也不动', () => {
  assert.equal(publicText('请寻找可核验的替代原始来源', {}), '请寻找可核验的替代原始来源')
  assert.equal(publicText('同配置价格', {}), '同配置价格')
  assert.equal(publicText('来源限制自动访问，保留URL及缺口；需其他公开原始来源', {}), '来源限制自动访问，保留URL及缺口；需其他公开原始来源')
  // 对象条目组件取的是 m.name，压根不经过 publicText（源码断言在第一条测试里）。
  const parameter = { name: '角转子最高转速', value: '22000', unit: 'r/min', direction: 'min', conditions: '角转子' }
  assert.equal(parameter.name, '角转子最高转速')
})

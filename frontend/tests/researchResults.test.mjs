/**
 * 检索结果页「检索来源」清单里那条 error_reason 的渲染契约。
 *
 * 那个位置原来是原样渲染 `{s.error_reason}`，一个翻译层都不过，而 error_reason 里嵌着
 * 后端拼进去的失败代号（procurement_research.py:1128 把 failure_kind 塞进括号）。
 * 全量实测：417 条会渲染的 error_reason 里有 43 条带代号，构成是
 *   empty_body 19 / RemoteDisconnected 9 / dns_not_found 7 / tls_* 7 / empty_shell 1。
 *
 * 修法是在调用点过 publicText 做**句内替换**，不是换成整句话术 —— 那句原文里有对用户
 * 有用的部分（「保留URL及原始失败原因待补查」），只能换代号，不能换句子。这份测试把
 * 这个契约钉住：改回原样渲染、或者换成 researchIssue(...).message，都会红。
 *
 * 数据来源：output/ 下全部 2112 个 json；下面 7 条 error_reason 原文照抄产物。
 */
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

import {publicText} from '../src/services/publicText.js'
import {researchIssue} from '../src/services/researchCopy.js'

const SUFFIX = '，其他来源可正常访问；保留URL及原始失败原因待补查'
const rendered = [
  'empty_body',
  'empty_shell',
  'dns_not_found',
  'RemoteDisconnected',
  'tls_BAD_ECPOINT',
  'tls_UNEXPECTED_EOF_WHILE_READING',
  'tls_CERTIFICATE_VERIFY_FAILED',
].map(code => `该来源连续两次无法读取（${code}）${SUFFIX}`)

const page = readFileSync(new URL('../src/pages/ResearchResults.jsx', import.meta.url), 'utf8')

test('来源清单的 error_reason 必须过翻译层，不能退回原样渲染', () => {
  assert.match(page, /import \{ publicText \} from '\.\.\/services\/publicText'/)
  assert.match(page, /publicText\(s\.error_reason, \{\}\)/)
  assert.ok(!/\{s\.error_reason\}/.test(page), 'error_reason 又变成原样渲染了')
})

test('这 7 条会渲染的 error_reason 全部不再含英文代号，且句子其余部分一字不动', () => {
  for (const reason of rendered) {
    const shown = publicText(reason, {})
    assert.ok(!/[A-Za-z]+_[A-Za-z_]+/.test(shown), shown)
    assert.ok(!shown.includes('RemoteDisconnected'), shown)
    assert.ok(shown.endsWith(SUFFIX), '句子的有用部分被改掉了：' + shown)
    assert.ok(shown.startsWith('该来源连续两次无法读取（'), shown)
  }
  // 逐条对一遍具体说法，避免只验证了「没有下划线」而措辞其实没落地。
  assert.equal(publicText(rendered[0], {}), `该来源连续两次无法读取（来源正文为空）${SUFFIX}`)
  assert.equal(publicText(rendered[3], {}), `该来源连续两次无法读取（连接被对方中断）${SUFFIX}`)
  assert.equal(publicText(rendered[2], {}), `该来源连续两次无法读取（域名解析失败）${SUFFIX}`)
  assert.equal(publicText(rendered[4], {}), `该来源连续两次无法读取（与来源网站的安全连接未建立）${SUFFIX}`)
  assert.equal(publicText(rendered[1], {}), `该来源连续两次无法读取（页面只有空壳）${SUFFIX}`)
})

test('这里不能用整句话术：researchIssue 会把句子的有用部分整段丢掉', () => {
  // 这条不是「顺便测一下」，是把选型理由钉住：researchCopy 那套是给列表项生成整句话的。
  for (const reason of rendered) {
    const whole = researchIssue(reason).message
    assert.ok(!whole.endsWith(SUFFIX), '整句话术居然保留了原句？' + whole)
    assert.ok(whole.length < publicText(reason, {}).length)
  }
})

test('调用点传的是空 state，翻译层不能因此抛错', () => {
  // ResearchResults 没有 candidates/documents，调用点传 {}。这一层只用到代号词典。
  assert.doesNotThrow(() => publicText(rendered[0], {}))
  assert.equal(publicText('', {}), '')
  assert.equal(publicText(null, {}), '')
  assert.equal(publicText(undefined, {}), '')
  assert.equal(publicText(rendered[0], {}), publicText(rendered[0], {candidates: [], documents: []}))
})

test('不含代号的 error_reason 一个字符都不动', () => {
  const clean = [
    '来源已不存在，保留URL及缺口',
    '页面过大，保留URL；请提供具体明细页',
    '来源限制自动访问，保留URL及缺口；需其他公开原始来源',
    '来源快照已保存，但正文没有可读文字（可能是扫描件或图片版）',
    '无法读取来源正文，保留线索待重试',
  ]
  for (const reason of clean) assert.equal(publicText(reason, {}), reason)
})

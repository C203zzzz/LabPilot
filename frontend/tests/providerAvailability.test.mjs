import test from 'node:test'
import assert from 'node:assert/strict'

import { connectionSummary, providerState, visionState } from '../src/services/providerAvailability.js'

/**
 * 真实返回：GET http://127.0.0.1:8019/api/v1/providers
 * {"status":"configured","items":[{"id":"reasoning","status":"configured"},
 *  {"id":"search","status":"configured"},{"id":"vision","status":"ready_to_test","label":"视觉识别"}]}
 * 状态取值来自 backend/api/routes.py:457-476。
 */
const LIVE = {
  status: 'configured',
  items: [
    { id: 'reasoning', status: 'configured', label: '思考模型' },
    { id: 'search', status: 'configured', label: '联网能力' },
    { id: 'vision', status: 'ready_to_test', label: '视觉识别' },
  ],
}

test('ready_to_test 是可用但未验证，不是未连接', () => {
  const vision = visionState(LIVE)
  assert.equal(vision.available, true)
  assert.equal(vision.verified, false)
  assert.equal(vision.label, '视觉模型可用 · 尚未验证')
  assert.ok(!vision.label.includes('未连接'))
})

test('configured 才是已验证', () => {
  const vision = visionState({ items: [{ id: 'vision', status: 'configured' }] })
  assert.equal(vision.available, true)
  assert.equal(vision.verified, true)
  assert.equal(vision.label, '视觉模型已连接')
})

test('真的没有适配器时才说未连接且禁用', () => {
  for (const status of ['unavailable', 'missing']) {
    const vision = visionState({ items: [{ id: 'vision', status }] })
    assert.equal(vision.available, false)
    assert.equal(vision.label, '视觉模型未连接')
  }
  assert.equal(visionState(undefined).available, false)
})

test('余额不足单独说清，并禁用', () => {
  const vision = visionState({ items: [{ id: 'vision', status: 'payment_required' }] })
  assert.equal(vision.available, false)
  assert.equal(vision.label, '视觉模型余额不足')
})

test('providerState 不把 ready_to_test 混成 configured', () => {
  const state = providerState(LIVE, 'vision')
  assert.equal(state.status, 'ready_to_test')
  assert.equal(state.configured, false)
  assert.equal(state.readyToTest, true)
  assert.equal(state.available, true)
  assert.equal(providerState(LIVE, 'reasoning').configured, true)
})

test('读不到连接状态时说的是配置有误，并保留后端原话', () => {
  // TaskContext.refreshProviderStatus 在 /providers 报错时把后端原话写进 error
  // （配置项写错会返回带 code 与人话的 502）。这时说"未连接"就是把
  // "配置写错了"讲成了"连不上"。
  const reason = '配置项 LAB_VISION_JSON_RETRIES 的值 9 超出允许范围 0～3（视觉识别失败后的重试次数）'
  const vision = visionState({ status: 'unavailable', items: [], error: reason })
  assert.equal(vision.available, false)
  assert.equal(vision.label, '视觉模型配置有误')
  assert.equal(vision.hint, reason)
  assert.ok(!vision.label.includes('未连接'))
})

test('顶栏读不到连接状态时也说配置有误，并保留后端原话', () => {
  // AppShell 顶栏原来在 catch 里直接落回"未连接"，与上面同一件事两种说法。
  // 判断沿用 visionState：看 error 有没有值（api.js 的 fetchProviderStatus 已经把
  // 404 与网络错误当成"服务未连接"正常返回，剩下的才是配置写错这类 502）。
  const reason = '配置项 LAB_VISION_JSON_RETRIES 的值 9 超出允许范围 0～3（视觉识别失败后的重试次数）'
  const summary = connectionSummary({ status: 'unavailable', items: [], error: reason })
  assert.equal(summary.problem, '配置有误')
  assert.equal(summary.error, reason)
  assert.ok(!summary.problem.includes('未连接'))
  assert.equal(summary.model, false)
  assert.equal(summary.search, false)
})

test('顶栏读得到状态时照旧分开说已连接/未连接', () => {
  const live = connectionSummary(LIVE)
  assert.equal(live.problem, '')
  assert.equal(live.error, '')
  assert.equal(live.model, true)
  assert.equal(live.search, true)
  // ready_to_test 不算已连接，但也不是配置有误。
  const partial = connectionSummary({ items: [{ id: 'reasoning', status: 'ready_to_test' }] })
  assert.equal(partial.problem, '')
  assert.equal(partial.model, false)
  assert.equal(partial.search, false)
})

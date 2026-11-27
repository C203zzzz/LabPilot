/**
 * 缺口 → 可直接发给销售的话术。
 *
 * 口径：把本轮真实缺口变成能直接复制出去的文字，点名要配置、税运、安装培训、保修、
 * 交付时间。已经说清的条件不再问（同一句问两遍，销售会以为系统没读他的回复）。
 *
 * fixture 取真跑那一单的形状：销售只给了「HIRAYAMA HVE-50，78,000 元，数量 1」，
 * 税、运费、安装培训、保修、交货时间与配置都没写（见 ``merge-audit-hve50`` 的
 * ``run.baseline``：这几个字段全是 null）。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import { QUESTION_DEFS, missingConditionKeys, supplierQuestionSheet } from '../src/services/supplierQuestionSheet.js'

const baseline = {
  brand: 'HIRAYAMA',
  model: 'HVE-50',
  supplier_price: 78000,
  currency: 'CNY',
  quantity: 1,
  configuration: null,
  tax: null,
  shipping: null,
  installation: null,
  warranty: null,
  service_response: null,
  service_onsite: null,
  delivery: null,
}

test('本轮缺的项按用户点名的几类挑出来（配置、税运、安装培训、保修、交付时间）', () => {
  const keys = missingConditionKeys({ baseline, prices: [] })
  for (const key of ['configuration', 'tax', 'installation', 'warranty', 'delivery']) {
    assert.ok(keys.has(key), `${key} 应该算缺项`)
  }
  const sheet = supplierQuestionSheet({ baseline, prices: [] })
  assert.deepEqual(sheet.missingLabels, ['配置', '税费与运费', '安装培训', '保修', '报修响应与上门', '交付时间'])
})

test('话术能直接复制：带称呼、带报价、逐条编号，六类条件都在', () => {
  const sheet = supplierQuestionSheet({ baseline, prices: [] })
  assert.match(sheet.body, /^您好，关于HIRAYAMA HVE-50（销售报价 78,000 元）的供货条件/)
  assert.match(sheet.body, /1\. 配置：/)
  assert.match(sheet.body, /2\. 税费与运费：/)
  assert.match(sheet.body, /3\. 安装培训：/)
  assert.match(sheet.body, /4\. 保修：/)
  assert.match(sheet.body, /5\. 报修响应与上门：/)
  assert.match(sheet.body, /6\. 交付时间：/)
  assert.match(sheet.body, /都没有写清，麻烦一并确认/)
  // 换行保留（页面用 <pre> 原样展示，粘出去不能挤成一行）。
  assert.ok(sheet.body.includes('\n1. '))
})

test('已经写清的条件不再问（同一句话不重复问两遍）', () => {
  const sheet = supplierQuestionSheet({
    baseline: { ...baseline, configuration: '主机含 6×50ml 角转子', warranty: '整机保修 2 年' },
    prices: [],
  })
  assert.ok(!sheet.missingLabels.includes('配置'))
  assert.ok(!sheet.missingLabels.includes('保修'))
  assert.ok(!sheet.body.includes('1. 配置：'), '已经说过的配置又被问了一遍')
  assert.ok(!sheet.body.includes('保修：主机与选件的质保期'))
  assert.match(sheet.body, /税费与运费：/)
})

test('成交记录里缺的条件也算本轮缺口（不只是销售报价里没写的）', () => {
  const keys = missingConditionKeys({
    baseline: { ...baseline, configuration: '已写清', tax: '含税13%', shipping: '包邮', installation: '含培训', warranty: '2 年', service_response: '4 小时', service_onsite: '24 小时', delivery: '4 周' },
    prices: [{ gaps: ['configuration', 'delivery', 'tax'] }],
  })
  assert.ok(keys.has('configuration') && keys.has('delivery') && keys.has('tax'))
  assert.ok(!keys.has('installation'))
})

test('一条都不缺时不说"没有缺口"，而是把口径落到书面确认', () => {
  const full = { ...baseline, configuration: '含 6×50ml 角转子', tax: '含税13%', shipping: '含运费', installation: '含培训', warranty: '2 年', service_response: '4 小时', service_onsite: '24 小时', delivery: '4 周' }
  const sheet = supplierQuestionSheet({ baseline: full, prices: [] })
  assert.deepEqual(sheet.missingLabels, [])
  assert.match(sheet.body, /本轮销售报价里这些条件已经提到过/)
  assert.equal(sheet.missing.length, 0)
})

test('金额与数量按台账原样说，缺字段时不抛异常', () => {
  const sheet = supplierQuestionSheet({ baseline: { brand: '某厂', model: 'YX-100', supplier_price: '12000' } })
  assert.match(sheet.body, /某厂 YX-100（销售报价 12,000 元）/)
  const bare = supplierQuestionSheet({})
  assert.match(bare.body, /^您好，关于这台设备的供货条件/)
  assert.deepEqual(supplierQuestionSheet().missingLabels, QUESTION_DEFS.map(def => def.label))
  assert.equal(missingConditionKeys().size, QUESTION_DEFS.length)
})

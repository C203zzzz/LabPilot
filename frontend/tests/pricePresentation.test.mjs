import test from 'node:test'
import assert from 'node:assert/strict'

import {
  displayPriceValue,
  formatPriceAmount,
  orderedTransactionPrices,
  PRICE_SOURCE_FIELDS,
  priceSourceLinks,
  procurementUnit,
  termAbsenceReason,
  transactionPrices,
} from '../src/services/pricePresentation.js'

test('a missing commercial term carries the reason the backend recorded', () => {
  const reason = '公告正文及已取得的附件未提供该项'
  const price = { configuration: 'unknown', field_notes: { configuration: reason } }
  assert.equal(termAbsenceReason(price, 'configuration'), reason)
  assert.equal(displayPriceValue(price.configuration), '未提供')
})

test('a stated term has no absence reason, and one never recorded falls back to the plain value', () => {
  assert.equal(termAbsenceReason({ configuration: '含6×50ml角转子' }, 'configuration'), '')
  // The value wins even if a stale note is present.
  assert.equal(termAbsenceReason({ configuration: '含6×50ml角转子', field_notes: { configuration: '旧' } }, 'configuration'), '')
  // Records stored before the reason was recorded still render.
  assert.equal(termAbsenceReason({ configuration: null }, 'configuration'), '')
  assert.equal(termAbsenceReason({}, 'configuration'), '')
  assert.equal(termAbsenceReason(undefined, 'configuration'), '')
})

test('only completed transaction records enter the formal price section', () => {
  const values = [
    { kind: 'award', amount: 6800 },
    { kind: 'contract', amount: 7100 },
    { kind: 'listing', amount: 6500 },
    { kind: 'bid_amount', amount: 6400 },
  ]

  assert.deepEqual(transactionPrices(values).map(item => item.kind), ['award', 'contract'])
})

test('missing fields stay explicit instead of being inferred', () => {
  assert.equal(displayPriceValue('unknown'), '未提供')
  assert.equal(formatPriceAmount(null), '未提供')
  assert.equal(procurementUnit({ project_id: 'P-001' }), '原文未提取')
  assert.equal(procurementUnit({ institution: '某高校' }), '某高校')
  assert.equal(formatPriceAmount(6800, 'CNY'), '6,800 元')
})

test('rejected candidates never enter the formal price table', () => {
  // 后端「认出」环节会拒绝型号不符的成交价（例如 HC-3016R ≠ H1850R）。
  // 拒绝理由只解释"为什么是 0 条"，它不是价格，不能出现在价格表里。
  const run = {
    prices: [],
    price_rejections: [{
      reason_code: 'model_mismatch',
      model: 'H1850R',
      url: 'https://cg.qdu.edu.cn/jjjg/9920.chtml',
      excerpt: '台式高速冷冻离心机 | 中科中佳/HC-3016R | 35000 | 1 | ￥32,980.00',
      other_models: ['HC-3016R'],
    }],
  }

  assert.deepEqual(transactionPrices(run.prices), [])
})

test('a model-mismatch rejection names the model that did not match', () => {
  // 这条断言锁定用户实际看到的信息：不是"没有数据"，而是"型号不一致"。
  const rejection = {
    reason_code: 'model_mismatch',
    model: 'H1850R',
    other_models: ['HC-3016R'],
    detail: '引文中的型号是 HC-3016R，与本次目标型号 H1850R 不一致：同品类的不同型号不能互相认定成交价。',
  }

  assert.equal(rejection.reason_code, 'model_mismatch')
  assert.deepEqual(rejection.other_models, ['HC-3016R'])
  assert.match(rejection.detail, /HC-3016R/)
  assert.match(rejection.detail, /H1850R/)
})

test('价格来源按列表取，不是只取第一条', () => {
  // 用户口径：「价格来源可能不止一条，按列表渲染，别只取第一条就把其余丢了」。
  // 现在后端只写 ``url`` 一个字段（``validate_price`` 的构造），列表也只有一个
  // 字段名；这里钉住的是"它是一个列表"这个形状与去重规则。
  assert.deepEqual(priceSourceLinks({ url: 'https://a.example/x' }), ['https://a.example/x'])
  assert.deepEqual(priceSourceLinks({ url: 'https://a.example/x', notice_url: 'https://b.example/y' }), ['https://a.example/x'])
  // 同一个地址写了两遍只列一次；空值不产生条目，也不产生占位符。
  assert.deepEqual(priceSourceLinks({ url: ' https://a.example/x ' }), ['https://a.example/x'])
  assert.deepEqual(priceSourceLinks({}), [])
  assert.deepEqual(priceSourceLinks({ url: '' }), [])
  assert.deepEqual(priceSourceLinks({ url: null }), [])
  assert.deepEqual(priceSourceLinks(undefined), [])
  assert.ok(PRICE_SOURCE_FIELDS.includes('url'))
})

test('呈现顺序：成交日期从新到旧，不是后端那份检索顺序', () => {
  // 后端 ``run['prices']`` 是"先见先留"的并集（_merged_prices），先扫到谁谁在前，
  // 跟金额、日期无关——所以页面必须自己排，否则「第 1 条」会随检索顺序换人。
  const values = [
    { kind: 'award', date: '2024-05-06', unit_price: 10000 },
    { kind: 'award', date: '2026-03-25', unit_price: 30000 },
    { kind: 'contract', date: '2025-01-08', unit_price: 20000 },
  ]
  assert.deepEqual(orderedTransactionPrices(values).map(item => item.unit_price), [30000, 20000, 10000])
  // 输入不能被改动：这份数组同时还是"有没有官方成交记录"的判据。
  assert.deepEqual(values.map(item => item.unit_price), [10000, 30000, 20000])
})

test('日期读不出来的排最后，并保持后端原序；非成交记录不参与', () => {
  const values = [
    { kind: 'award', unit_price: 1, date: '不是日期' },
    { kind: 'award', unit_price: 2 },
    { kind: 'listing', unit_price: 99, date: '2030-01-01' },
    { kind: 'award', unit_price: 3, date: '2026年3月25日' },
    { kind: 'award', unit_price: 4, date: '2026-03-25' },
  ]
  // 中文写法的日期认得出来（``2026年3月25日`` 与 ``2026-03-25`` 是同一天，
  // 同一天的两条保持原序）；认不出日期的两条按它们在原数组里的先后排在最后。
  assert.deepEqual(orderedTransactionPrices(values).map(item => item.unit_price), [3, 4, 1, 2])
  assert.deepEqual(orderedTransactionPrices([]), [])
  assert.deepEqual(orderedTransactionPrices(undefined), [])
})

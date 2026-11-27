/**
 * 价格栏空着时的交代。
 *
 * 数字全部来自真跑台账 ``output/e2e-final/runs/merge-audit-hve50/evidence/16-final-run.json``
 * （usage / provider_failure_counts / unavailable_search_providers / provider_result_totals /
 * republished_prices / price_source_skips）。业务口径是不留白：查了什么、为什么没有，
 * 一次说清；**不许**拿"同品牌同品类成交价"或"第三方报价表"填空（用户 2026-10-06 否决）。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  PRICE_SKIP_REASON_LABELS,
  priceEmptyDisclosure,
  priceSkipReasonLabel,
  searchChannelLabel,
} from '../src/services/priceEmptyDisclosure.js'

const run = {
  usage: { search: 47, fetch: 78, model: 19, education_platform: 3, province_award: 6 },
  provider_failure_counts: { exa_mcp: 0, deepseek_web: 0, public_web: 0, baidu: 3, tavily: 0, ccgp: 0 },
  unavailable_search_providers: ['baidu'],
  provider_result_totals: { exa_mcp: 18, deepseek_web: 6, public_web: 5, tavily: 5, ccgp: 11 },
  republished_prices: new Array(9).fill({ url: 'https://www.120shop.cn/i758911491' }),
  price_source_skips: [
    ...new Array(41).fill({ skip_reason: 'no_amount_evidence' }),
    ...new Array(30).fill({ skip_reason: 'price_source_limit' }),
    ...new Array(15).fill({ skip_reason: 'body_not_fetched' }),
    ...new Array(7).fill({ skip_reason: 'source_type_not_price_source' }),
    ...new Array(3).fill({ skip_reason: 'target_model_not_in_body' }),
  ],
}

const textOf = disclosure => disclosure.rows.map(row => `${row.label}：${row.value}`).join('\n')

test('查了多少、几个通道：次数来自 usage，通道数来自失败计数字段', () => {
  const shown = textOf(priceEmptyDisclosure(run))
  assert.match(shown, /检索 47 次/)
  assert.match(shown, /抓取正文 78 次/)
  assert.match(shown, /模型调用 19 次/)
  assert.match(shown, /检索通道共 6 个/)
})

test('通道产出逐条点名（计数口径写清楚：是"有返回的调用次数"，不是金额条数）', () => {
  const shown = textOf(priceEmptyDisclosure(run))
  assert.match(shown, /Exa 检索 18 次有返回/)
  assert.match(shown, /中国政府采购网直查 11 次有返回/)
  assert.match(shown, /不是金额条数/)
})

test('失败或限流的通道要点名，并说清它已被隔离', () => {
  const shown = textOf(priceEmptyDisclosure(run))
  assert.match(shown, /百度检索失败 3 次/)
  assert.match(shown, /本轮已被隔离，不再调用/)
  // 没有失败的通道不许被写成失败（0 计数的通道一个都不点名）。
  assert.ok(!shown.includes('Tavily 检索失败'))
})

test('转载价标明是二手来源，挂牌价标明不是成交价且不能用于判断', () => {
  const shown = textOf(priceEmptyDisclosure(run))
  assert.match(shown, /聚合站转载价：9 条/)
  assert.match(shown, /二手来源|第二手金额/)
  assert.match(shown, /不能当作成交价/)
  assert.match(shown, /平台挂牌价等被跳过的来源：96 条来源没有进入核价/)
  for (const label of Object.values(PRICE_SKIP_REASON_LABELS)) assert.ok(shown.includes(label), `${label} 没有点名`)
  assert.match(shown, /41 条/)
  assert.match(shown, /不是成交价，不能用于判断/)
})

test('结论一句：没有可采信成交价，无法判断报价是否合理；也不代表别处没有', () => {
  const shown = textOf(priceEmptyDisclosure(run))
  assert.match(shown, /本轮没有取得可采信的同型号成交价/)
  assert.match(shown, /无法判断这份销售报价是否合理/)
  assert.match(shown, /不代表这台机器在别处没有成交记录/)
})

test('这一块只解释"为什么空"，不许新增别的价格口径填空', () => {
  const shown = textOf(priceEmptyDisclosure(run))
  for (const forbidden of ['同品牌同品类', '第三方报价表', '建议区间', '市场均价']) {
    assert.ok(!shown.includes(forbidden), `出现了被用户否决的口径：${forbidden}`)
  }
})

test('台账缺字段时给空结构而不是抛异常，结论仍然给出来', () => {
  const empty = priceEmptyDisclosure({})
  assert.ok(Array.isArray(empty.rows))
  const shown = textOf(empty)
  assert.match(shown, /聚合站转载价：0 条/)
  assert.match(shown, /平台挂牌价等被跳过的来源：0 条/)
  assert.match(shown, /本轮没有取得可采信的同型号成交价/)
  assert.deepEqual(priceEmptyDisclosure(null).failed, [])
})

test('通道名与跳过原因都有人话，认不出来的原样印出来不隐藏', () => {
  assert.equal(searchChannelLabel('baidu'), '百度检索')
  assert.equal(searchChannelLabel('some_new_channel'), 'some_new_channel')
  assert.equal(searchChannelLabel(''), '未命名通道')
  assert.equal(priceSkipReasonLabel('no_amount_evidence'), '正文里没有金额证据')
  assert.equal(priceSkipReasonLabel('brand_new_reason'), 'brand_new_reason')
  const shown = textOf(priceEmptyDisclosure({ price_source_skips: [{ skip_reason: 'explain_me' }] }))
  assert.match(shown, /explain_me/)
})

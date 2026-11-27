/**
 * 产品资料型号优先之后，界面上怎么把「比较基准」和「销售口述的报价」说开。
 *
 * 这里的每一条都对应后端真实会写进 draft / baseline 的字段：
 *   quote_model / quote_brand 由 procurement_intake.with_document_suggestions 写进 draft，
 *   再由 procurement_research.confirm 的 allowed 白名单带进 baseline；
 *   document_identity 由同一次读取写在 intake 状态上。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  documentBasisConflict,
  documentIdentityLead,
  modelKey,
  quoteAttribution,
  quotedModelOf,
} from '../src/services/procurementIdentity.js'

const spoken = { brand: '上海和泰', model: 'Master-S15', supplier_price: 38000, currency: 'CNY' }
const documentBasis = { brand: '和泰 Hitech', model: 'RO DI400', supplier_price: 38000, currency: 'CNY',
  quote_model: 'Master-S15', quote_brand: '上海和泰' }

test('型号写法差异（空格、连字符、大小写）不算换了型号', () => {
  assert.equal(modelKey('RO DI400'), modelKey('ro-di400'))
  assert.equal(quotedModelOf({ ...documentBasis, quote_model: 'ro di400' }), '')
})

test('报价挂在口述型号上时，页面明说这笔报价不属于比较基准', () => {
  assert.equal(quotedModelOf(documentBasis), 'Master-S15')
  const text = quoteAttribution(documentBasis)
  assert.ok(text.includes('Master-S15'), text)
  assert.ok(text.includes('RO DI400'), text)
  assert.ok(text.includes('不是本次比较基准'), text)
})

test('没有跨型号的报价时不产生任何归属说明', () => {
  assert.equal(quoteAttribution(spoken), '')
  assert.equal(quoteAttribution({ ...spoken, quote_model: 'Master-S15' }), '')
  assert.equal(quoteAttribution({}), '')
})

test('资料型号覆盖口述型号时，对话开头的说明同时给出两台机器', () => {
  const lead = documentIdentityLead({ brand: '和泰 Hitech', model: 'RO DI400',
    spoken_brand: '上海和泰', spoken_model: 'Master-S15', overrides_spoken: true })
  assert.ok(lead.includes('RO DI400'), lead)
  assert.ok(lead.includes('Master-S15'), lead)
  assert.ok(lead.includes('待核实的销售推荐'), lead)
})

test('资料只是补上空缺时，说明里不出现「改了」的口径', () => {
  const lead = documentIdentityLead({ brand: 'B', model: 'PH10', spoken_model: '', overrides_spoken: false })
  assert.ok(lead.includes('PH10'), lead)
  assert.ok(!lead.includes('改为'), lead)
})

test('已确认基准与资料型号不一致时提示重新确认，一致或没有基准时不提示', () => {
  const conflict = documentBasisConflict({ brand: '和泰 Hitech', model: 'RO DI400' }, spoken)
  assert.ok(conflict.includes('RO DI400'), conflict)
  assert.ok(conflict.includes('Master-S15'), conflict)
  assert.ok(conflict.includes('重新确认'), conflict)
  assert.equal(documentBasisConflict({ model: 'Master-S15' }, spoken), '')
  assert.equal(documentBasisConflict({ model: 'RO DI400' }, {}), '')
  assert.equal(documentBasisConflict(null, spoken), '')
})

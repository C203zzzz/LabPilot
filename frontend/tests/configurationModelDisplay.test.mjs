/**
 * 候选卡片上的「配置所属型号」（设计规范：**抽到的配置属于哪个型号就展示
 * 哪个型号**）。
 *
 * 数据由后端 迭代规范 算好（``candidate.configuration_model``，判不出是空串；依据在
 * ``configuration_model_basis``）。这一层只管显示，三态各有自己的用例：
 *
 * 1. 有值、与候选自己的型号**不同** → 印出一行，两个写法都在
 *    （真例：``bekomat30`` 的配置抽自 ``BBS-V600`` 那页，``设计规范`` §1.1）；
 * 2. 有值、归一后**相同** → 一个字都不印（同一件事不重复说）；
 * 3. 空串或字段缺失 → 不印（判不出就是不猜，也不拿候选自己的型号顶上）。
 *
 * 除纯函数外，这里把 ``ResearchResults`` 真渲染出来数文本——正式候选与参考候选共用
 * 同一张卡片，所以两条路都钉一条。
 */
import assert from 'node:assert/strict'
import {after, test} from 'node:test'
import {fileURLToPath} from 'node:url'

import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {createServer} from 'vite'

import {configurationModelLine, configurationModelNotice} from '../src/services/configurationModel.js'

const server = await createServer({
  root: fileURLToPath(new URL('..', import.meta.url)),
  server: {middlewareMode: true}, appType: 'custom', logLevel: 'error',
})
const {default: ResearchResults} = await server.ssrLoadModule('/src/pages/ResearchResults.jsx')
after(() => server.close())

const CATALOG = 'https://www.olaibo.com/ProductDetail/634.html'

const candidate = (extra = {}) => ({
  candidate_id: '1f2e3d4c5b6a7089', brand: '济南欧莱博科学仪器有限公司', model: 'bekomat30',
  category: '洁净工作台', url: CATALOG, excerpt: '洁净工作台-欧莱博bekomat30',
  configuration_url: 'https://www.olaibo.com/ProductDetail/630.html',
  configuration: '型号 技术参数 | BBS-V600\n洁净等级 | ISO 5\n平均风速 | 0.3m/s',
  price_lookup: {status: 'not_requested', reason_code: 'not_requested'}, prices: [],
  ...extra,
})

const render = run => renderToStaticMarkup(React.createElement(ResearchResults, {
  run: {prices: [], sources: [], lanes: {}, gaps: [], alternatives: [], reference_candidates: [], ...run},
  baseline: {brand: '博迅', model: 'BJ-1CD', category: '洁净工作台'}, section: 'alternatives',
}))

test('配置所属型号与候选型号不同时：两个写法都印出来', () => {
  const notice = configurationModelNotice(candidate({configuration_model: 'BBS-V600'}))
  assert.deepEqual(notice, {model: 'BBS-V600', original: 'bekomat30'})
  assert.equal(configurationModelLine(notice), '配置所属型号：BBS-V600（原候选写法：bekomat30）')
})

test('归一后相同就不印（全角/大小写/空白/连字符不算不同）', () => {
  assert.equal(configurationModelNotice(candidate({configuration_model: 'bekomat30'})), null)
  assert.equal(configurationModelNotice(candidate({configuration_model: 'BEKOMAT-30'})), null)
  assert.equal(configurationModelNotice(candidate({configuration_model: ' bekomat 30 '})), null)
  assert.equal(configurationModelNotice(candidate({model: 'LMQ.C-80E', configuration_model: 'lmq.c‐80e'})), null)
})

test('空串或没有这一格都不印（判不出就是不猜）', () => {
  assert.equal(configurationModelNotice(candidate({configuration_model: ''})), null)
  assert.equal(configurationModelNotice(candidate()), null)
  assert.equal(configurationModelNotice(null), null)
  assert.equal(configurationModelLine(null), '')
})

test('正式候选卡片：不同时印出那一行，候选自己的型号没被抹掉', () => {
  const html = render({alternatives: [candidate({configuration_model: 'BBS-V600'})]})
  assert.ok(html.includes('配置所属型号：BBS-V600（原候选写法：bekomat30）'), html.slice(0, 400))
  assert.ok(html.includes('bekomat30'), '候选卡片上的标题型号被抹掉了')
  assert.equal(html.split('配置所属型号').length - 1, 1, '这一行只该印一次')
})

test('正式候选卡片：相同时不印（不重复、不加噪音）', () => {
  const same = render({alternatives: [candidate({configuration_model: 'bekomat30'})]})
  assert.ok(!same.includes('配置所属型号'), same.slice(0, 400))
  const empty = render({alternatives: [candidate({configuration_model: ''})]})
  assert.ok(!empty.includes('配置所属型号'), empty.slice(0, 400))
  const legacy = render({alternatives: [candidate()]})
  assert.ok(!legacy.includes('配置所属型号'), legacy.slice(0, 400))
})

test('参考候选用同一张卡片，这条口径同样生效', () => {
  const different = render({reference_candidates: [candidate({
    configuration_model: 'BBS-V600', reference_reason: 'same_brand',
  })]})
  assert.ok(different.includes('配置所属型号：BBS-V600（原候选写法：bekomat30）'), different.slice(0, 400))
  const same = render({reference_candidates: [candidate({
    configuration_model: 'bekomat30', reference_reason: 'same_brand',
  })]})
  assert.ok(!same.includes('配置所属型号'), same.slice(0, 400))
})

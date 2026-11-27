/**
 * 「配置」那一格到底显示什么：厂家参数与采购要求**互斥**。
 *
 * 背景：中标公告只写型号、品牌、数量、单价，从不写技术参数，所以「配置」那一格本来
 * 只能是空的；空着的那一格下面又并排着一块采购要求（``TenderConfig``），读的人很容易
 * 把采购方对投标产品开出的门槛当成这台机器的标称参数。后端现在会去这个型号自己的
 * 产品参数页把参数取回来，并用 ``configuration_source`` 标明它不是公告写的；这一层
 * 负责两份信息只显示一份：有产品参数就显示产品参数，没有才显示采购要求。
 *
 * 这里不靠「正则匹配组件源码」下结论——真的把 ``ResearchResults`` 渲染出来，数渲染
 * 结果里有没有那两块（vite 的 ``ssrLoadModule`` 转 JSX，``react-dom/server`` 出 HTML，
 * 直接跑在 ``node --test`` 里，不需要浏览器）。
 */
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {after, test} from 'node:test'
import {fileURLToPath} from 'node:url'

import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {createServer} from 'vite'

import {PRODUCT_SPEC_SOURCES, productSpec} from '../src/services/pricePresentation.js'

const page = readFileSync(new URL('../src/pages/ResearchResults.jsx', import.meta.url), 'utf8')
const tender = JSON.parse(readFileSync(new URL('./fixtures/tender-requirements.json', import.meta.url), 'utf8'))

//: 真实厂家页上那段参数的开头（``xiangyilxj.com/product/info_itemid_952.html``，
//: 网窗口 2400 字）。这里只用前几行验「两块互斥」的接线；**全文**在
//: ``fixtures/xka-2200-manufacturer-spec.txt``，由 ``productSpecLayout.test.mjs`` 检查。
const SPEC_TEXT = [
  '技术参数',
  '型号 | XKA-2200',
  '最高转速 | 4500r/min',
  '最大相对离心力 | 2000×g',
  '外形尺寸 | 456×330×266mm(L×W×H)',
  '重量 | 12kg',
].join('\n')
const SPEC_URL = 'https://www.xiangyilxj.com/product/info_itemid_952.html'

const server = await createServer({
  root: fileURLToPath(new URL('..', import.meta.url)),
  server: {middlewareMode: true}, appType: 'custom', logLevel: 'error',
})
const {default: ResearchResults} = await server.ssrLoadModule('/src/pages/ResearchResults.jsx')
after(() => server.close())

function record(overrides = {}) {
  return {
    kind: 'award', model: 'XKA-2200', brand: '湘仪', unit_price: 17480, currency: 'CNY',
    date: '2026-03-25', url: 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm',
    excerpt: '品牌：湘仪\n规格型号：XKA-2200\n数量：1\n单价(元)：17480', gaps: [], ...overrides,
  }
}

const render = price => renderToStaticMarkup(React.createElement(ResearchResults, {
  run: {prices: [price], tender_requirements: tender, sources: [], lanes: {}, alternatives: []},
  baseline: {brand: '湘仪', model: 'XKA-2200'}, section: 'price',
}))

const TENDER_HEADING = '招标文件对这台设备的配置要求'
const SPEC_HEADING = '该型号产品资料的标称参数（非成交配置）'

// ---------------------------------------------------------------------------
// 取数：什么样的配置才算产品资料上的参数
// ---------------------------------------------------------------------------

test('a product-page configuration brings its provenance with it', () => {
  const spec = productSpec(record({configuration: SPEC_TEXT, configuration_source: 'manufacturer_spec',
                                   configuration_url: SPEC_URL}))
  assert.equal(spec.label, '厂家资料')
  assert.equal(spec.host, 'www.xiangyilxj.com')
  assert.equal(spec.url, SPEC_URL)
  assert.equal(spec.text, SPEC_TEXT)
  assert.equal(productSpec(record({configuration: SPEC_TEXT, configuration_source: 'product_page_spec'})).label, '产品资料')
  assert.deepEqual(Object.values(PRODUCT_SPEC_SOURCES), ['厂家资料', '产品资料'])
})

test('a configuration that came from the notice is never shown as product data', () => {
  // 既有历史记录里 configuration 可能有值，但没有来源标记——那不是产品参数。
  assert.equal(productSpec(record({configuration: '含6×50ml角转子'})), null)
  assert.equal(productSpec(record({configuration: SPEC_TEXT, configuration_source: 'unknown'})), null)
  assert.equal(productSpec(record({configuration: 'unknown', configuration_source: 'manufacturer_spec'})), null)
  assert.equal(productSpec(record({configuration: '', configuration_source: 'manufacturer_spec'})), null)
  assert.equal(productSpec(record()), null)
  assert.equal(productSpec({}), null)
  assert.equal(productSpec(undefined), null)
  assert.equal(productSpec(record({configuration: SPEC_TEXT, configuration_source: 'manufacturer_spec'})).url, '')
})

// ---------------------------------------------------------------------------
// 渲染：两份信息只出现一份
// ---------------------------------------------------------------------------

test('with product-page parameters the card shows them and not the tender requirements', () => {
  const html = render(record({configuration: SPEC_TEXT, configuration_source: 'manufacturer_spec',
                              configuration_url: SPEC_URL}))
  assert.ok(html.includes(SPEC_HEADING), '厂家参数块没渲染')
  // 参数现在按网格渲染：键和值分成 <dt>/<dd> 两格，不再是「键 | 值」一行。
  assert.ok(html.includes('<dt>最高转速</dt><dd>4500r/min</dd>'), '参数格子没渲染')
  // 出处必须印出来；印的是中文站点名，不是 www.xiangyilxj.com 这种英文域名
  // （业务口径：不要把链接本身展示出来）。
  assert.ok(html.includes('湘仪集团官网'), '出处没印出来')
  assert.ok(!html.includes(TENDER_HEADING), '两块同时出现了，这正是要修的混淆')
  assert.ok(!html.includes('▲1'), '采购要求的条款跟着出来了')
})

test('without a product page the card shows the tender requirements, exactly as before', () => {
  const html = render(record({configuration: 'unknown'}))
  assert.ok(html.includes(TENDER_HEADING))
  // 那句「不等于这台机器的产品标称参数」必须原样保留。
  assert.ok(html.includes('不等于这台机器的产品标称参数'))
  assert.ok(!html.includes(SPEC_HEADING), '没有产品参数却渲染了产品参数块')
})

test('a record that wrote its own configuration still does not show a spec block', () => {
  // 成交记录自己写了配置（合同附件里的那种）时，不冒充产品资料；采购要求块照旧。
  const html = render(record({configuration: '含6×50ml角转子'}))
  assert.ok(html.includes(TENDER_HEADING))
  assert.ok(!html.includes(SPEC_HEADING))
})

test('the caveat text lives in the tender block, not in the product block', () => {
  // 产品参数块说的是另一种话：它明确否认这是成交配置。迭代规范（2026-10）把原来那串三重
  // 否定收成一句，但**否认本身必须还在**——「标称参数 ≠ 成交配置」是这个块的立身之本。
  const html = render(record({configuration: SPEC_TEXT, configuration_source: 'manufacturer_spec'}))
  assert.ok(html.includes('不是成交配置'))
  assert.ok(!html.includes('不是政府采购公告里写的配置'), '啰嗦的三重否定又回来了')
})

test('the two blocks are wired as one either-or, not as two neighbours', () => {
  // 渲染已经证明了两块互斥；这条钉住接线本身，return 变成并列渲染会红。
  // ``ProductSpecCard``（「该型号产品资料的标称参数（非成交配置）」）现在**只有成交价
  // 这一处**：候选卡片上那一处 2026-10-07 下线了（口径与量测见
  // ``candidateRoster.test.mjs`` 里那条）。这里钉住的是"成交价这一处仍是
  // ``spec``／``TenderConfig`` 二选一"，而不是"两处都在"。
  assert.equal((page.match(/<TenderConfig/g) || []).length, 1)
  assert.equal((page.match(/<ProductSpecCard/g) || []).length, 1)
  assert.match(page, /\{!spec && <TenderConfig tender=\{tender\} model=\{price\.model\} \/>\}/)
  assert.match(page, /\{spec && <ProductSpecCard spec=\{spec\} \/>\}/)
  assert.ok(!page.includes('ProductSpecCard spec={spec} anchors={anchors}'),
            '候选卡上的那一处又回来了')
})

test('the internal source codes never reach the page', () => {
  // 两个取值只活在 service 层；页面用 spec.label 印中文。
  // 查的是**代码**：注释里为了解释出处会提到字段名，那不是渲染出去的内容。
  const code = page.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '')
  assert.ok(!code.includes('manufacturer_spec'))
  assert.ok(!code.includes('product_page_spec'))
  assert.ok(!code.includes('configuration_source'))
  assert.match(page, /\{spec\.label\}/)
})

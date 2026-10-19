import React, { useState } from 'react'
import SourceLink, { safeWebUrl } from '../components/SourceLink'
import { ActionButton, StatusTag } from '../components/ui'
import ResearchIssues from '../components/ResearchIssues'
import { displayPriceValue as text, formatPriceAmount, hasDisplayValue, hasProcurementUnit, MARKET_REFERENCE_FIELD_LEGEND, marketReferenceBadge, marketReferenceBlock, MARKET_REFERENCE_TIERS, orderedTransactionPrices, priceLeadGroups, priceLeadsForSection, priceRangeText, priceSourceLinks, procurementUnit, productSpec, transactionPrices } from '../services/pricePresentation'
import { siteNameOf, sourceSummary } from '../services/siteNames'
import { splitTenderClauses, tenderDeviceForModel, tenderSource } from '../services/tenderPresentation'
import { alternativePresentation, brandGroupKey, candidateTitle, configMatchPresentation, sameBrandAsBaseline } from '../services/alternativePresentation'
import { applyPriceLookup, beginLookup, endLookup, priceButtonState, priceLookupView } from '../services/alternativePriceLookup'
import { candidateSourceEntries } from '../services/candidateSources'
import { publicText } from '../services/publicText'
import { productSpecLayout } from '../services/productSpecLayout'
import { groupSources } from '../services/sourceGrouping'

const labels = {historical_configuration:'同型号历史成交', historical_model:'同型号日期价格参考', model_reference:'同型号历史成交参考'}
const sourceLabels = {manufacturer_website:'厂家官网', manufacturer_download:'厂家资料', tender_document:'采购公告 / 合同', dealer:'经销商', aggregator:'仪器门户 / 转载', unknown:'发布者未核实'}
const laneLabels = {completed:'本轮检索结束', failed:'检索中断', pending:'尚未执行', running:'正在检索'}
const compareLabels = {satisfied:'满足', violated:'不满足', unknown:'缺少证据'}
//: 参考候选为什么没进深入核验（后端 ``reference_reason``）；文案原样保留，只是从
//: 渲染处内联的对象提成常量——参考候选现在渲染成和正式候选同一张卡片，这句话要
//: 单独放在卡片上方，内联在 JSX 里读不动。
const REFERENCE_REASON_LABELS = {same_brand:'属于原品牌，保留为同品牌参考。',brand_unresolved:'品牌信息不完整，暂不能判断是否跨品牌。',brand_already_represented:'本轮已核验该品牌的另一个型号，优先把名额留给其他品牌。',duplicate_model:'重复型号线索。',lead_below_requirement:'线索显示参数可能低于要求，未进入深入核验。',verification_limit:'本轮核验名额已用完，尚未深入核验。',no_conclusion:'已进入深入核验名单，但这一轮没有跑出结论（线索低于要求、沿用上一轮结果，或这一步失败被跳过），按参考候选列出。'}

/**
 * What the tender demanded of this instrument, folded into the price card.
 *
 * The clause text is printed bare (row.text) on purpose: backend statement
 * repeats the instrument name and the 「不等于标称参数」 caveat on every single
 * clause, which reads as noise once the card already says it once. The caveat
 * and the provenance live in the paragraph above the list, not on each line.
 */
function TenderConfig({ tender, model }) {
  const device = tenderDeviceForModel(tender, model)
  const requirements = device?.requirements || []
  if (!requirements.length) return null
  const {key, plain} = splitTenderClauses(requirements)
  const source = tenderSource(tender)
  const row = item => <li key={item.line ?? `${item.clause}-${item.text}`}>
    <strong className="tender-clause-number">{item.clause}</strong>
    <span>{item.text}</span>
  </li>
  return <div className="price-tender-config">
    <h5>招标文件对这台设备的配置要求（{requirements.length} 条，其中 ▲ {key.length} 条）</h5>
    <p className="tender-origin">以下要求来自{source.title || '招标文件'}{source.chapter ? `（${source.chapter}）` : ''}，是采购方对投标产品开出的门槛，中标人须满足；它不等于这台机器的产品标称参数，也不是这笔成交记录里写的配置。{source.url ? <SourceLink url={source.url}>查看招标文件原文</SourceLink> : null}</p>
    {key.length > 0 && <ul className="tender-clauses">{key.map(row)}</ul>}
    {plain.length > 0 && <details className="tender-plain-clauses"><summary>一般条款（{plain.length} 条）</summary><ul className="tender-clauses">{plain.map(row)}</ul></details>}
  </div>
}

/**
 * This machine's own nominal parameters, read off a product page.
 *
 * 招标文件那些条款是「采购方要什么」，这里印的是「这台机器有什么」——两种信息
 * 同时出现，读的人会把门槛当本机参数。所以这一块与 ``TenderConfig`` 互斥：
 * 有产品参数就只显示产品参数，没有才显示采购要求（见 ``PriceRecord``）。
 * 出处必须印出来，因为这段文字不是政府采购公告背书的。
 *
 * 它是**一张价格记录卡里的第二块**（用户口径：型号价格和配置是同级的，同在一张
 * 卡里），与第一块、和后面的「来源」共处一张卡；自成一块靠底色与左边框区分，
 * 不靠"另开一张卡"。结构化交给 ``productSpecLayout``——那段 ``configuration`` 是
 * 整页正文（参数 + 同一张表按行重复 + 尾部"相关推荐"的产品名和裸链接），原来用
 * ``<pre>`` 原样铺出来就是用户说的"这个配置清单太丑了"。现在按分组出网格、
 * 多列表出真表格，解析不出来的行不铺开；表格的列间隔与数值列右对齐在
 * ``styles/index.css`` 的 ``.product-spec-table`` 那组规则里，数值列由
 * ``productSpecLayout`` 逐列标出来（``table.numeric``，按数据判、不按表头猜）。
 */
function ProductSpecCard({ spec }) {
  const layout = productSpecLayout(spec.text)
  // 说明句里原来内联着「查看{label}原文」的链接。现在配置来源的链接统一放在
  // 卡尾那块「来源」里（用户口径：来源默认折叠、展开后给出价格来源与配置来源
  // 两条），同一句话里再挂一遍就是同一个地址印两次。**这句防混淆的话一个字都没
  // 丢**——三层区分（是厂家标称值／不是政府采购公告里写的配置／不是招标文件对
  // 投标产品提出的门槛）原样保留，砍掉的只是「以下参数」这个指代（它由上面那个
  // <h4> 和下面那张参数网格的**位置**承担）和重复的主语「它」、冗余的定语「自己的」。
  // 出处里的站点名换成中文（原来印的是 ``www.xiangyilxj.com`` 这种英文域名）。
  return <section className="product-spec-card" aria-label="厂家标称参数（非公告内容）">
    <h4>厂家标称参数（非公告内容）</h4>
    <p className="tender-origin">{spec.label}{spec.url ? `（${siteNameOf(spec.url)}）` : ''}上这个型号的标称值，不是政府采购公告里写的配置，也不是招标文件对投标产品提出的门槛。</p>
    {!layout.groups.length && <p className="product-spec-empty">厂家资料正文里没有可对齐的参数条目，请打开原文核对。</p>}
    {layout.groups.map((group, index) => <section className="product-spec-group" key={`${group.title || 'parameters'}-${index}`}>
      {group.title && <h5 className="product-spec-group-title">{group.title}</h5>}
      {group.rows.length > 0 && <dl className="price-target-terms product-spec-grid">{group.rows.map((row, at) => <div key={`${row.key}-${at}`}><dt>{row.key}</dt><dd>{row.value}</dd></div>)}</dl>}
      {group.table && group.table.rows.length > 0 && <div className="comparison-scroll product-spec-table" tabIndex={0} role="region" aria-label={`${group.title || '厂家资料'}表格`}>
        <table>
          <thead><tr>{group.table.headers.map((header, at) => <th scope="col" className={group.table.numeric?.[at] ? 'is-numeric' : undefined} key={`${header}-${at}`}>{header}</th>)}</tr></thead>
          <tbody>{group.table.rows.map((row, at) => <tr key={at}>{row.map((cell, column) => <td className={group.table.numeric?.[column] ? 'is-numeric' : undefined} key={column}>{cell}</td>)}</tr>)}</tbody>
        </table>
      </div>}
    </section>)}
    {layout.notes.length > 0 && <p className="product-spec-note">{layout.notes.join(' ')}</p>}
  </section>
}

/**
 * 一张价格记录卡的「来源」层：价格从哪来、配置从哪来，两条分开写清楚。
 *
 * 这是用户要的第三层（原话：「再下一层是显示来源（需点击显示 默认隐藏）；点击后
 * 提供公告的链接（价格来源）和厂家信息页的链接（配置来源）配置来源可以是卖场或
 * 聚合站」）。四个约束落在这里：
 *
 * 1. 默认折叠（``<details>`` 不带 ``open``），但**标题上写出每类各有几条**——
 *    折叠藏的是地址，不能让人以为这条记录没有来源；
 * 2. 两条链接各有自己的标签（价格来源 / 配置来源），一眼能分开；
 * 3. **链接文字是"指向哪里"的简述**（「中国政府采购网 · 中标公告」「湘仪集团官网 ·
 *    产品页」），不是 URL、也不是英文域名——用户口径：不要把链接本身展示出来；
 * 4. 没有地址的条目一个都不渲染（不印"未提供链接"），两条都没有时整层不存在。
 *    地址还要过 ``safeWebUrl``：字段里存的可能不是可点的 HTTP(S) 地址，那种
 *    情况下 ``SourceLink`` 会退化成一段文字，看着像链接却打不开。
 * 5. **每条只有一行**：``<dt>`` 已经写明这是哪一类来源，行内不再重述一遍
 *    （原来是「价格来源」＋「这条成交金额的原始出处：」＋链接、「配置来源」＋
 *    「这段配置参数来自厂家资料，不是公告内容。出处：」＋链接——同一件事一行里
 *    说两遍）。配置那一层"不是公告内容"由卡里的 ``ProductSpecCard`` 说明句和
 *    它的标题负责，这里只回答"配置是从哪个地址来的"。
 */
//: 成交记录的类型 → 页面类型。后端 ``kind`` 只有 award / contract 两种会走到这里
//: （``transactionPrices`` 只放这两种），认不出来时给中性的"公告页面"。
const PRICE_PAGE_TYPES = { award: '中标公告', contract: '采购合同公告' }

function PriceSources({ price, spec }) {
  const entries = []
  for (const url of priceSourceLinks(price)) {
    if (!safeWebUrl(url)) continue
    const page = PRICE_PAGE_TYPES[price.kind] || '公告页面'
    entries.push({ key: `price-${url}`, label: '价格来源', url, page, text: sourceSummary(url, page) })
  }
  // 配置来源可以是厂家自有页，也可以是卖场或聚合站的产品页——具体是哪一种由后端
  // 写在这条记录上的来源标记决定，页面只印它算好的中文名（``spec.label``）。
  if (spec && spec.url && safeWebUrl(spec.url)) {
    entries.push({ key: `configuration-${spec.url}`, label: '配置来源', url: spec.url, page: '产品页',
      text: sourceSummary(spec.url, '产品页') })
  }
  if (!entries.length) return null
  const tally = [['价格', entries.filter(entry => entry.label === '价格来源').length],
    ['配置', entries.filter(entry => entry.label === '配置来源').length]]
    .filter(([, count]) => count)
    .map(([name, count]) => `${name} ${count}`)
    .join(' · ')
  return <details className="price-record-sources">
    <summary>来源（{tally}）</summary>
    <dl className="price-source-list">{entries.map(entry => <div key={entry.key}>
      <dt>{entry.label}</dt>
      <dd><SourceLink url={entry.url} page={entry.page}>{entry.text}</SourceLink></dd>
    </div>)}</dl>
  </details>
}

/**
 * 候选卡尾部的「来源」：**照成交价卡片那一块（``PriceSources``）做**，不另造样式。
 *
 * 三处一致：同一套类名（``price-record-sources`` / ``price-source-list``）、同一种
 * 一行式（``<dt>`` 中文标签 + ``<dd>`` 一条"站点名 · 页面类型"的可点链接，``href``
 * 仍是完整地址、页面上不印 URL）、同样默认折叠、同样跳过不可点的地址
 * （``safeWebUrl``——字段里可能不是可点的 HTTP(S) 地址）。
 *
 * 数据全部来自后端 ``candidate.source_pages``（列表，标了主来源），前端不判读、不补数；
 * 没有这个字段的老数据退回 ``candidate.url``（见 ``candidateSourceEntries``）。
 * 这一块取代了原来那行单独的「查看型号来源」——同一个入口不在卡片上出现两次。
 */
function CandidateSources({ candidate }) {
  const entries = candidateSourceEntries(candidate).filter(entry => safeWebUrl(entry.url))
  if (!entries.length) return null
  return <details className="price-record-sources">
    <summary>来源（{entries.length}）</summary>
    <dl className="price-source-list">{entries.map(entry => <div key={entry.key}>
      <dt>{entry.primary ? `${entry.label}（主来源）` : entry.label}</dt>
      <dd><SourceLink url={entry.url} page={entry.page}>{entry.text}</SourceLink></dd>
    </div>)}</dl>
  </details>
}

function PriceRecord({ price, tender, position }) {
  const gapLabels = {brand:'品牌',configuration:'配置',quantity:'数量',tax:'税费',shipping:'运费',installation:'安装培训',warranty:'保修',service_response:'报修响应',service_onsite:'上门处理',delivery:'交付',date:'公告日期'}
  const gaps = [...new Set([...(price.gaps || []), ...Object.keys(gapLabels).filter(key => price[key] == null || price[key] === '' || price[key] === 'unknown')])].map(key => gapLabels[key] || key)
  // 公告里写了什么就显示什么。拿不到的字段不再逐个印「原文未提取 / 未提供」——
  // 把缺失铺满整张卡片，看不出这条记录到底提供了哪些事实。
  const supplier = price.supplier || price.seller || price.winner || price.awardee || ''
  const meta = [
    ['采购单位', hasProcurementUnit(price) ? procurementUnit(price) : ''],
    ['供应商', hasDisplayValue(supplier) ? text(supplier) : ''],
    ['成交日期', hasDisplayValue(price.date) ? price.date : ''],
    ['采购数量', hasDisplayValue(price.quantity) ? text(price.quantity) : ''],
    ['项目编号', hasDisplayValue(price.project_id) ? text(price.project_id) : ''],
  ].filter(([, value]) => value)
  // 配置优先用产品资料上的标称参数：中标公告从不写技术参数，那一格空着的时候
  // 旁边只剩采购要求，用户很容易把它读成这台机器的参数。两样同时出现就是这个
  // 混淆的来源，所以它们互斥：有产品参数就显示产品参数、不显示采购要求块；
  // 没有产品参数才显示采购要求（它仍然是独立的、写清了不是标称参数的一块）。
  const spec = productSpec(price)
  // 配置统一看招标文件；只有招标文件里没有这台设备时，才回落到公告自己写的配置。
  const fromTender = Boolean(tenderDeviceForModel(tender, price.model))
  const terms = [['configuration','配置'],['tax','税费'],['shipping','运费'],['installation','安装培训'],['warranty','保修'],['service_response','报修响应'],['service_onsite','上门处理'],['delivery','交付']]
    .filter(([key]) => hasDisplayValue(price[key]) && !(key === 'configuration' && (fromTender || spec)))
  // 卡内三块的排法（用户口径：型号价格和配置是**同级的**，同在一张卡里）：
  //   第一块 型号与价格并排一行（原来型号单独占一行、在价格下面）；
  //   第二块 配置——有产品资料时是 ``ProductSpecCard``，没有时是配置格与
  //          ``TenderConfig``（两者互斥，见 ``productSpec`` 那段的说明）；它仍在
  //          这里、和第一块同一张卡里，不再是并列的第二张卡；
  //   第三块（卡尾）「来源」，默认折叠，展开后给出价格来源与配置来源两条链接。
  // 同一个型号命中多条成交记录时，每张卡右上角给一个「第 n 条 / 共 m 条」——
  // 编号是调用方（``position``）算好的：只有一条时不印（一条记录谈不上第几条），
  // 区块标题 「可比历史成交记录」 在 ``prices.map`` 外面、只渲染一次，这里绝不
  // 再加一遍标题（用户明确抱怨过「又重复一次可比历史成交记录」）。
  return <>
    <article className="procurement-result price-record">
      <div className="price-record-heading">
        <div className="price-record-identity">
          <span className="price-record-model-label">成交型号</span>
          <strong className="price-record-model">{text(price.model)}</strong>
          <h4>{formatPriceAmount(price.unit_price, price.currency)}</h4>
          <span className="price-record-level">{labels[price.comparison_level || price.level] || '历史成交参考'}</span>
        </div>
        {position && position.total > 1 && <span className="price-record-ordinal">第 {position.index} 条 / 共 {position.total} 条</span>}
      </div>
      {meta.length > 0 && <dl className="transaction-meta">{meta.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>}
      {terms.length > 0 && <dl className="transaction-terms">{terms.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{text(price[key])}</dd></div>)}</dl>}
      {!spec && <TenderConfig tender={tender} model={price.model} />}
      {spec && <ProductSpecCard spec={spec} />}
      <p className="price-boundary">这是同型号历史成交记录，不是当前报价。{gaps.length ? `与销售报价有差异或缺失：${gaps.join('、')}。请以原文为准。` : '供货条件需重新确认。'}</p>
      {price.delta != null && <p>销售报价与此条同型号历史成交的差额：{Number(price.delta).toLocaleString('zh-CN')} 元</p>}
      <details><summary>查看金额原文与计算口径</summary><blockquote>{price.excerpt}</blockquote><p>{price.calculation}</p></details>
      <PriceSources price={price} spec={spec} />
    </article>
  </>
}

/**
 * One row per model, not one row per web page.
 *
 * A single model is often hit by a dozen leads (different configurations,
 * sellers, forwarded records) and every row repeated the same caveat. The row
 * now carries the amount range and the count; the individual sources unfold.
 */
function PriceLead({ group }) {
  const items = group.leads
  const primary = items[0]
  const note = lead => lead.note || '金额口径、发布方或交易状态尚未核实，不能作为成交价。'
  return <article className="price-lead">
    <div className="price-lead-heading">
      <div><strong>{text(group.model)} · {priceRangeText(group)}</strong><span>{items.length > 1 ? `${items.length} 条线索` : '非成交证据'}</span></div>
      <SourceLink url={primary.url}>查看线索来源</SourceLink>
    </div>
    <p>{note(primary)}</p>
    {items.length > 1
      ? <details className="price-lead-list"><summary>展开这 {items.length} 条线索来源</summary>
        {items.map((lead, index) => <div className="price-lead-item" key={`${lead.url}-${index}`}>
          <div className="price-lead-item-heading"><strong>{formatPriceAmount(lead.amount, lead.currency, lead.amount_unit)}</strong><SourceLink url={lead.url}>查看线索来源</SourceLink></div>
          <p>{note(lead)}</p>
          {lead.excerpt && <details><summary>查看线索原文</summary><blockquote>{lead.excerpt}</blockquote></details>}
        </div>)}
      </details>
      : primary.excerpt && <details><summary>查看线索原文</summary><blockquote>{primary.excerpt}</blockquote></details>}
  </article>
}

/**
 * Say why a candidate price was NOT adopted.
 *
 * A correct refusal used to be indistinguishable from "we found nothing": the
 * 「认出」 step drops a model-mismatched award (a sibling model priced far from
 * the target) and the user only saw 0 条. These reasons
 * come from the backend predicate that actually rejected the row, so they are
 * an explanation of the gate, never a new price.
 */
const rejectionLabels = {
  model_mismatch: '型号不一致',
  brand_mismatch: '品牌不一致',
  budget_not_achieved: '只是预算或限价',
  total_not_unit_price: '只是项目总金额',
  not_award_evidence: '不是成交证据',
  date_ungrounded: '成交日期无法追溯',
  amount_not_unit_price: '金额口径不是单价',
  // 代号是 `unparsed_amount`（后端 procurement_rules.UNPARSEABLE_AMOUNT 的值）。
  // 这里原来写成 `unparseable_amount`，于是这类被拒记录前端印的是代号本身。
  unparsed_amount: '金额写法无法解析',
  seller_self_description: '卖家自述页面',
  source_not_tender: '来源不是采购公告',
  body_unavailable: '未取得正文',
  excerpt_not_in_source: '引文无法回原文',
  // 下面两个代号原来没有条目，前端会把 `not_awarded_candidate` 原样印给用户。
  not_awarded_candidate: '不是中标候选人清单',
  quantity_unverified: '采购数量未核实',
  unexplained: '未命中已知原因',
}

function PriceRejectionSection({ rejections, targetModel, adoptedCount = 0 }) {
  const list = rejections || []
  if (!list.length) return null
  const grouped = list.reduce((acc, item) => {
    const code = item.reason_code || 'unexplained'
    acc[code] = acc[code] || []
    acc[code].push(item)
    return acc
  }, {})
  return <details className="price-rejection-section">
    <summary>为什么这些价格没有被采用（{list.length} 条被拒）</summary>
    <p>下面每一条都是本轮抓到、但没有通过单价核验的金额。列出来是为了说明{adoptedCount ? '哪些金额没有进入正式成交价比较' : '“为什么是 0 条”'}，不是把这些金额当作成交价。</p>
    {Object.entries(grouped).map(([code, items]) => <div className="rejection-group" key={code}>
      <h4>{rejectionLabels[code] || '其他原因（未分类）'}（{items.length} 条）</h4>
      {items.map((item, index) => <article className="price-lead" key={`${item.url}-${index}`}>
        <div className="price-lead-heading">
          <div>
            <strong>{item.model ? `${item.model === targetModel ? '目标型号' : '拒收记录所属型号（非本次目标）'} ${text(item.model)}` : '型号未标注'}</strong>
            <span>{rejectionLabels[code] || '其他原因（未分类）'}</span>
          </div>
          <SourceLink url={item.url}>查看被拒来源</SourceLink>
        </div>
        <p>{item.detail}</p>
        {item.other_models?.length > 0 && <p>引文中出现的型号：{item.other_models.join('、')}</p>}
        {item.snapshot_sha256 && <p className="price-boundary">抓取时的页面快照已留存；校验值在随报告导出的来源清单里。</p>}
        {item.excerpt && <details><summary>查看被拒原文</summary><blockquote>{item.excerpt}</blockquote></details>}
      </article>)}
    </div>)}
  </details>
}

function MarketReferenceCard({ lead }) {
  // 来源域名、页面自述的日期和买卖双方仍然逐条印出来——事实一条都没少。
  // 被拿掉的是逐格重复的那两句说明：「（页面自述）」原来在日期、采购方、
  // 供货方后面各挂一遍，一张卡三遍，现在由块头的表头统一声明一次；卡尾那行
  // 「页面本身不是原始公告」同理，收进块级说明，逐行只留一个短标记。
  return <article className="price-lead market-reference-lead">
    <div className="price-lead-heading">
      <div>
        <strong>{text(lead.model)} · {formatPriceAmount(lead.amount, lead.currency, lead.amount_unit)}</strong>
        <span className="market-reference-badge">{marketReferenceBadge(lead)}</span>
      </div>
      <SourceLink url={lead.url}>查看来源页面</SourceLink>
    </div>
    <dl className="transaction-meta">
      <div><dt>来源域名</dt><dd>{lead.host || '未识别'}</dd></div>
      {lead.date && <div><dt>页面标注日期</dt><dd>{lead.date}</dd></div>}
      {lead.buyer && <div><dt>采购方</dt><dd>{lead.buyer}</dd></div>}
      {lead.seller && <div><dt>供货 / 成交方</dt><dd>{lead.seller}</dd></div>}
    </dl>
    {lead.excerpt && <details><summary>查看金额原文</summary><blockquote>{lead.excerpt}</blockquote></details>}
  </article>
}

/**
 * 本轮没有官方成交记录时，网页上的金额是这台机器唯一的交易信号。
 *
 * ``marketReferenceBlock`` 在有官方成交记录时返回 ``null``，这里就整块不渲染——
 * 那种情况下这些页面属于"历史招标公告"之外的商业线索，前端一个字都不承载，
 * 既不出现域名，也不出现「聚合站」「转载」「镜像」这类字样或条数说明。
 * 没有官方记录时它正常展开（不是折叠、也不是只给个数量），并在块头写明它
 * 不是原始公告、不能当成交价。
 *
 * 那句警示（``warning``）**只在这里印一次**，加粗、带底色，位置在区块最顶部
 * （标题那一行之下、层级说明与任何一条线索之前）。逐条线索不再重复它——那样 7 条
 * 线索就是 7 遍，用户明确不要（「不能重复展示 比如每一条价格都展示出这个提醒」）。
 * 原来和它挤在同一段里的两条事实挪到 ``explanation``，同样只说一次。
 */
function MarketReferenceSection({ leads, values }) {
  const block = marketReferenceBlock(leads, values)
  // 一次别铺太多行：展开后超过 10 行先只给前 10 行（原来就有的止血措施，别丢）。
  const [expanded, setExpanded] = useState(false)
  if (!block) return null
  const { title, warning, explanation, count, badge } = block
  const head = expanded ? block.leads : block.leads.slice(0, 10)
  const rest = block.leads.slice(10)
  const tierGroups = [1, 2, 3].map(tier => [tier, block.leads.filter(lead => lead.tier === tier)]).filter(([, group]) => group.length)
  return <section className="result-section market-reference-section" aria-label={title}>
    <div className="market-reference-block">
      <div className="market-reference-top">
        <h3>{title}</h3>
        <span className="market-reference-badge">{badge}</span>
      </div>
      <p className="market-reference-warning">{warning}</p>
      <p className="market-reference-explanation">{explanation}</p>
      {/* 层级说明每个块一次。原来每张卡上那行整句的「转发记录，原始公告未核实」
          换成这里的类别标记，逐行只在标题里带一个短标记。 */}
      {tierGroups.map(([tier, group]) => <p className="market-reference-tier-note" key={tier}><strong>{MARKET_REFERENCE_TIERS[tier].title}（{group.length} 条 · 标记「{marketReferenceBadge(group[0])}」）</strong>{MARKET_REFERENCE_TIERS[tier].summary}</p>)}
      <p className="market-reference-legend">{MARKET_REFERENCE_FIELD_LEGEND}</p>
      <div className="market-reference-rows">
        {head.map((lead, index) => <MarketReferenceCard key={`${lead.url}-${lead.model}-${lead.amount}-${index}`} lead={lead} />)}
      </div>
      {!expanded && rest.length > 0 && <button type="button" className="link-button" onClick={() => setExpanded(true)}>显示其余 {rest.length} 条线索</button>}
      {expanded && rest.length > 0 && <button type="button" className="link-button" onClick={() => setExpanded(false)}>收起</button>}
      <p className="price-boundary">这 {count} 条线索没有进入正式成交价比较，也不参与销售报价是否合理的判断。</p>
    </div>
  </section>
}

function SourceBody({ source }) {
  const [open, setOpen] = useState(false)
  // 每行来源下面原来都挂一个「查看已抓取正文」。上面那行状态已经写了「已读取正文」，
  // 同一件事在同一行里说了两遍；标题只留「正文原文」，正文入口仍是一行来源一个。
  return <details className="source-body-details" onToggle={event => setOpen(event.currentTarget.open)}><summary>正文原文</summary>{open && <div className="source-body"><p>网页提取内容；读取成功不代表来源身份和全部参数已核验。</p><pre>{source.body_text}</pre></div>}</details>
}

function ModelExcerpt({ candidate, sources }) {
  const source = sources.find(s => s.url === candidate.url && s.body_status === 'fetched')
  if (!source?.body_text || !candidate.model) return null
  const model = candidate.model.normalize('NFKC').replace(/[‐‑–—]/g, '-').replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const matcher = new RegExp(`(^|[^a-zA-Z0-9])${model}($|[^a-zA-Z0-9])`, 'i')
  const lines = source.body_text.split('\n').filter(line => matcher.test(line.normalize('NFKC').replace(/[‐‑–—]/g, '-')))
  const table = lines.filter(line => line.startsWith('[表格按列展开'))
  const excerpts = (table.length ? table : lines).slice(0, 10)
  if (!excerpts.length) return null
  return <details className="model-source-excerpt"><summary>已读到的型号与参数原文（{excerpts.length} 行）</summary><p>{['manufacturer_website','manufacturer_download'].includes(source.source_type) ? '以下为来源中的型号相关原文；是否满足要求仍以逐项核验为准。' : '已取得网页正文，但发布者身份尚未核实。以下仅供核对，不作为厂家确认或可替代结论。'}</p><blockquote>{excerpts.join('\n')}</blockquote><SourceLink url={source.url}>打开完整来源</SourceLink></details>
}

const verdictTagTones = {success: 'status-success', warning: 'status-warning', danger: 'status-danger', neutral: 'status-neutral', muted: 'status-muted'}

/**
 * 一张替代候选卡片（两段式：先展示候选，用户点「查这个的价格」才查这一个）。
 *
 * 卡片上每一块的数据来源都是后端已经写好的字段，前端不判读、不补数：
 * - 型号与品牌 → ``candidate.model`` / ``candidate.brand``；品牌没解析出来时印
 *   ``品牌未解析``（``candidateTitle``），**不留空**；
 * - 逐项差异 → ``candidate.config_match.items``（后端 ``configuration_alignment``），
 *   一行一项，判「一致／不同／未知」；未知那一栏的字面就是"正文里没写"；
 * - 已核验比例 / 数值接近程度 → ``config_match.confidence`` / ``config_match.soft_score``，
 *   两者为 ``null`` 时印"未算出 + 为什么"，**不印 0%**；
 * - 差额 → 不满足项自己的 ``shortfall_text``（后端 ``shortfall_text`` 那句人话的
 *   唯一来源）；后端没挂就留空，前端不按 percent 拼句子；
 * - 原文行 → ``candidate.excerpt``（发现这一条时读到的那句原文）与逐项证据的
 *   ``observed.evidence.excerpt`` + 其 ``url``；
 * - 价格 → 候选自己的 ``price_lookup``（后端 ``alternative_price_lookup``：没查／
 *   查了没有／有来源但没被采信三态分开说）；点过按钮之后改用这一次响应的结果。
 *
 * 「查这个的价格」按钮的状态与点击逻辑都在 ``services/alternativePriceLookup.js``
 * 里（纯函数，node --test 直接断言）。**这一张卡的查价只影响这一张卡**：结果按
 * ``candidate_id`` 写进 ``lookupResults``，别的卡片收到的 props 一个字没变。
 */
function Alternative({ candidate, baseline, sources, reference = false, officialPrices = [], lookupResults = {}, pendingLookups = {}, onLookupPrice }) {
  const rows = candidate.matrix || []
  // 判据与核价页、报告、来源清单同一个：**这个任务**有没有官方成交记录
  // （``run.prices`` 里的 award/contract），不是这个候选自己的 ``candidate.prices``
  // ——候选的价格依据常常只有挂牌价，用它当判据会让同一页上两处结论相反
  // （核价页判定"有官方记录"，候选卡片却把线索列出来）。
  const block = marketReferenceBlock(candidate.price_leads || [], officialPrices)
  const { verdict, groups, provisionalNote, category } = alternativePresentation(candidate, { baseline, reference })
  const config = configMatchPresentation(candidate)
  // 候选定位键：接口按 ``candidate_id`` 查价（``sha256(identity(model)+url)[:16]``）。
  // **不退回数组下标**——候选名单一变，下标就指到别人头上；没有编号的老数据宁可不给按钮。
  const candidateId = String(candidate.candidate_id || '').trim()
  const lookup = candidateId ? lookupResults[candidateId] : null
  const priceView = priceLookupView(candidate, lookup)
  const button = priceButtonState({ candidateId, pending: pendingLookups,
                                    status: lookup ? lookup.status : String(candidate.price_lookup?.status || '') })
  // 候选卡片里这一小串「价格依据」和核价页那块同一个毛病：一个型号好几条记录
  // 原样往下堆、谁也不说是第几条。用同一个 ``orderedTransactionPrices``（成交日期
  // 从新到旧）与同一个 ``position``，两处编号的算法只有一份。
  // 点过按钮之后用**这一次响应里的价格**：价格区立刻更新，整页不重取。
  const prices = lookup ? lookup.prices : (candidate.prices || [])
  const evidence = orderedTransactionPrices(prices)
  return <article className="procurement-result alternative-result">
    <div className="result-heading"><h4>{candidateTitle(candidate)}</h4></div>
    <div className={`alternative-verdict verdict-${verdict.tone}`}>
      <div className="verdict-line">
        <strong className="verdict-label">{verdict.label}</strong>
        {verdict.scoreTag && <span className={`status-tag ${verdictTagTones[verdict.tone] || 'status-neutral'}`}>{verdict.scoreTag}</span>}
      </div>
      {verdict.detail && <p className="verdict-detail">{verdict.detail}</p>}
      {verdict.basis && <p className="verdict-basis">{verdict.basis}</p>}
      {verdict.boundary && <p className="verdict-boundary">{verdict.boundary}</p>}
    </div>
    <p className="alternative-category">品类：{category}</p>
    {groups.length > 0 && <dl className="alternative-facts">{groups.map(group => <div className={`alternative-fact fact-${group.tone}`} key={group.key}>
      <dt>{group.label}</dt>
      <dd>{group.items.map(item => <span className="fact-item" key={item.name}>{item.name}{item.note && <small>{item.note}</small>}</span>)}{group.note && <small className="fact-note">{group.note}</small>}</dd>
    </div>)}</dl>}
    {/* 逐项差异（两段式设计里的第二条：Octopart 那种一项一行，不给单一百分比）。
        数据来自 ``config_match.items``——候选**详情页正文**里读到的配置对齐。下面那张
        「逐项比较参数」走跨品牌核验的来源，两者不是同一批证据，所以这块自报来源，
        免得同一张卡上两套数字并排出现时读者不知道信哪个（同一处口径见
        ``procurement_research.alternative_report_item`` 那段注释）。 */}
    {config.present && <section className="alternative-config-match" aria-label="与已确认要求的逐项差异">
      <h5>与已确认要求的逐项差异（来自候选详情页正文{config.rows.length ? `，共 ${config.rows.length} 项` : ''}）</h5>
      {config.rows.length > 0 && <p className="config-match-legend">一项一行，不给单一百分比。「未知」的是正文里没写，既不算满足、也不算不满足；下面那张「逐项比较参数」来自跨品牌核验，与本表不是同一批证据。</p>}
      {config.emptyNote && <p className="result-note">{config.emptyNote}</p>}
      {config.rows.length > 0 && <div className="comparison-scroll" tabIndex={0} role="region" aria-label={`${candidate.model || '候选'}与已确认要求的逐项差异`}>
        <table className="config-match-table">
          <thead><tr><th>指标</th><th>已确认要求</th><th>正文里读到的</th><th>判断</th></tr></thead>
          <tbody>{config.rows.map(row => <tr key={row.key} className={`config-match-row config-${row.state}`}>
            <th scope="row">{row.name}</th>
            <td>{row.required}</td>
            <td>{row.observed}</td>
            <td>
              <StatusTag tone={row.tone === 'muted' ? 'neutral' : row.tone}>{row.stateLabel}</StatusTag>
              {/* 差额：后端在不满足项上挂了 ``shortfall_text`` 才印；没挂就留空，
                  前端不按百分比自己拼一句话（同一句文案的唯一来源在后端）。 */}
              {row.shortfallText && <small className="config-match-note">{row.shortfallText}</small>}
              {row.evidenceUrl && <SourceLink url={row.evidenceUrl}>这一项的原文</SourceLink>}
              {row.excerpt && <blockquote>{row.excerpt}</blockquote>}
            </td>
          </tr>)}</tbody>
        </table>
      </div>}
      <dl className="config-match-scores">
        <div><dt>已核验比例</dt><dd>{config.confidence.text || '未算出'}{config.confidence.note && <small>{config.confidence.note}</small>}</dd></div>
        <div><dt>数值接近程度</dt><dd>{config.softScore.text || '未算出'}{config.softScore.note && <small>{config.softScore.note}</small>}</dd></div>
      </dl>
    </section>}
    {provisionalNote && <p className="result-note">{provisionalNote}</p>}
    {candidate.excerpt && <blockquote className="alternative-excerpt">{candidate.excerpt}</blockquote>}
    <CandidateSources candidate={candidate} />
    <ModelExcerpt candidate={candidate} sources={sources} />
    <details><summary>逐项比较参数（{rows.length} 项）</summary><div className="comparison-scroll" tabIndex={0} role="region" aria-label={`${candidate.model}参数比较`}><table><thead><tr><th>指标</th><th>原型号要求</th><th>查得参数</th><th>判断与依据</th></tr></thead><tbody>{rows.map((row,index) => <tr key={index}><th scope="row">{row.required.name}<small>{row.required.conditions}</small></th><td>{text(row.required.value)} {row.required.unit}</td><td>{row.observed ? `${text(row.observed.value)} ${row.observed.unit || ''}` : row.raw_evidence?.length ? '已有相关原文，待人工核对' : '未取得证据'}{row.observed?.conditions && <small>{row.observed.conditions}</small>}{row.raw_evidence?.map((e,i) => <blockquote key={i}>{e.raw_value != null && <small>识别值：{text(e.raw_value)}{e.unit ? ` ${e.unit}` : ''}{e.conditions ? `；条件：${e.conditions}` : ''}</small>}<br />{e.excerpt} <SourceLink url={e.url}>打开原文</SourceLink></blockquote>)}</td><td>{row.conflict ? '原文冲突' : row.raw_evidence?.length ? '有原文待核对' : compareLabels[row.status] || '缺少证据'}{row.evidence_status === 'provisional' && <small>临时正文证据，来源待核实</small>}{row.observed?.url && <SourceLink url={row.observed.url}>参数原文</SourceLink>}{row.conflict && row.observations?.map((o,i) => <p key={i}>{o.excerpt} <SourceLink url={o.url}>冲突来源</SourceLink></p>)}</td></tr>)}</tbody></table></div></details>
    {/* 候选自己的 ``candidate.price_leads``：这一档里有没有官方成交记录，用的还是
         ``marketReferenceBlock`` 那一个判据（``candidate.prices`` 里的 award/contract）。
         ——没有官方成交记录时这些线索正常列出，它是这个型号唯一的交易信号；有官方
         成交记录时整块不存在，`block` 为 null，域名与字样一个都不出现。 */}
    {block && <div className="alternative-leads"><h5>这个型号的价格线索（{block.count} 条）</h5><p>网页上写着的金额，出处未核实，不进入正式成交价比较。</p>{priceLeadGroups(block.leads).map(group => <PriceLead key={group.model} group={group} />)}</div>}
    {candidate.stronger_parameters?.length > 0 && <p>同条件下优于基准：{candidate.stronger_parameters.map(p => p.name).join('、')}。这不代表整体更优。</p>}
    {candidate.preference_checks?.length > 0 && <details><summary>其他功能要求</summary>{candidate.preference_checks.map((p,i) => <div key={i}><p>{p.preference}：{p.note}</p>{p.evidence?.map((e,n) => <p key={n}>{e.excerpt} <SourceLink url={e.url}>功能来源</SourceLink></p>)}</div>)}</details>}
    {/* 价格区 + 两段式的那个按钮：**先展示候选，用户点哪个才查哪个**。
        状态文案由 ``priceLookupView`` 把后端 ``price_lookup`` 的三态说成人话
        （没查／查了没有／有来源但没被采信——"没查"与"查了没有"必须能分开看，
        见 docs/148 与 backend/services/alternative_price_lookup.py 模块开头）；
        按钮的 disabled 与请求形状由 ``priceButtonState`` / ``lookupCandidatePrice``
        负责，点击只 POST 路径里那一个 ``candidate_id``。 */}
    <section className="alternative-price" aria-label={`${candidate.model || '这个候选'}的价格`}>
      <div className="alternative-price-heading">
        <h5>这个候选的价格</h5>
        <ActionButton variant="secondary" icon={null} disabled={button.disabled} onClick={() => { if (onLookupPrice) onLookupPrice(candidateId) }}>{button.label}</ActionButton>
      </div>
      <p className={`alternative-price-status price-${priceView.tone}`} role="status">{priceView.label}</p>
      {priceView.note && <p className="alternative-price-note">{priceView.note}</p>}
      {button.hint && <p className="action-hint">{button.hint}</p>}
      {evidence.length ? <details><summary>该型号价格依据（{evidence.length} 条）</summary>{evidence.map((p,i) => <PriceRecord key={`${p.url || p.model || 'price'}-${i}`} price={p} position={{index: i + 1, total: evidence.length}} />)}</details> : <p className="result-note">没有已核实的单价，暂不能比较采购成本。</p>}
    </section>
  </article>
}

export default function ResearchResults({ run, baseline, section, onCandidatePrice }) {
  const [sourceFilter, setSourceFilter] = useState('all')
  // 按需查价（两段式）：只保存"用户点过按钮的那几个候选"的结果与进行中状态，
  // 都按 ``candidate_id`` 分键。**点一个候选不会动其它候选的任何数据**——
  // 这就是"每次只查一个、其它候选的按钮照常可用"在页面上落地的位置
  // （``applyPriceLookup`` 只改一个键，其它条目按引用原样返回）。没有编号的老数据
  // 不给按钮，也不退回数组下标（下标会在候选名单变化后指错人）。
  const [lookupResults, setLookupResults] = useState({})
  const [pendingLookups, setPendingLookups] = useState({})
  const [lookupError, setLookupError] = useState('')
  const lookupPrice = async candidateId => {
    if (!candidateId || typeof onCandidatePrice !== 'function') return
    setPendingLookups(current => beginLookup(current, candidateId))
    setLookupError('')
    try {
      const result = await onCandidatePrice(candidateId)
      if (result) setLookupResults(current => applyPriceLookup(current, candidateId, result))
    } catch (error) {
      setLookupError(error?.message || '这次查价没有完成，请稍后重试。')
    } finally {
      setPendingLookups(current => endLookup(current, candidateId))
    }
  }
  // 呈现顺序与判据分开：``prices`` 保持后端顺序，只用来判"有没有官方成交记录"、
  // 数几条；``ordered`` 是给页面看的顺序（成交日期从新到旧，见
  // ``orderedTransactionPrices``——后端那份 ``run['prices']`` 的顺序是检索顺序，
  // 不是价格顺序）。编号取自 ``ordered``，排序变了几号就是几号。
  const prices = transactionPrices(run.prices || [])
  const ordered = orderedTransactionPrices(run.prices || [])
  const alternatives = run.alternatives || []
  // 品牌分组：判据在后端（见 services/alternativePresentation.js 的两个函数），
  // 这里只按后端给的 brand_key 归组、按 same_brand_as_baseline 分成"跨品牌/原品牌"。
  const cross = alternatives.filter(a => a.brand && !sameBrandAsBaseline(a, baseline))
  const same = alternatives.filter(a => a.brand && sameBrandAsBaseline(a, baseline))
  const unknown = alternatives.filter(a => !a.brand)
  const brands = [...new Set(cross.map(brandGroupKey))]
  const leads = priceLeadsForSection(run, alternatives, section)
  const groups = priceLeadGroups(leads)
  // 有官方成交记录时，来源清单只留后端判定为权威来源的那些（``authority`` 是
  // ``official_channel`` 或 ``manufacturer``：官方历史招标公告及其发布机构、机构自有
  // 官网、厂家资料）；没有官方记录时全部列出，因为那时商业线索是唯一的参考。
  // 判据由后端 ``source_authority`` 按已有分类给出，前端不按域名猜、也不手工维护
  // 域名表。``groupSources`` 拿同一个字段分组，两处不会各说一套。
  const listedOnly = prices.length > 0
  const sources = [...new Map((run.sources || [])
    .filter(s => s.url && (!listedOnly || (s.authority && s.authority !== 'unlisted')))
    .map(s => [s.url, s])).values()]
  const filtered = sources.filter(s => sourceFilter === 'all' || (sourceFilter === 'procurement' ? s.purpose === 'tender' || s.source_type === 'tender_document' : sourceFilter === 'unread' ? s.body_status !== 'fetched' : ['manufacturer_website','manufacturer_download'].includes(s.source_type)))
  const grouped = groupSources(filtered)
  // 缺口文本里嵌着触发它的页面地址（``run.gaps`` 里就有聚合站的详细页），那一列
  // 说明是后端算出来的事实，一字不改；有官方成交记录时只把不列出的域名挡在渲染外。
  const visibleUrls = listedOnly ? new Set(sources.map(s => s.url)) : null
  // 每张候选卡片都要的那三件（按需查价）：结果表、进行中表、点击入口。
  // 三张清单（跨品牌/原品牌/品牌不全）传的是**同一份**表——所以点 A 卡片只会让 A
  // 的状态变，B 卡片的 props 一个字没动，它的按钮照样能点。
  const priceCards = { lookupResults, pendingLookups, onLookupPrice: lookupPrice }
  return <>
    {/* 这一节两句话各说一遍同一件事的两处，都按"信息量大的那处保留、另一处只留独有的话"处理：
        1）收录判据（品牌＋完整型号＋单价＋公告日期）由上面这段引言给出，它在**有成交记录时
           也要在**（空态那时不渲染），所以判据留在引言里；空态改成指回上面，不再重抄一遍。
        2）"不能判断这份报价是否合理"是后端 ``run.price_assessment`` 的正式结论，出口在核价页
           的「分析结论」卡（ProcurementPanel）。结论卡有结论（``run.price_assessment`` 非空）
           时，空态不再自己下这个判断，改成指到那张卡，用户不会找不到结论；结论卡还是默认
           引导语时（价格为空的运行/老数据），空态才自己把这句说出来。 */}
    {section !== 'alternatives' && <section className="result-section transaction-section" aria-label="可比历史成交记录"><h3>可比历史成交记录</h3><p>收录标准：品牌、完整型号、单价、公告日期都能在原文中查到。条件不齐的仍会列出并标注缺口。</p>
      {!prices.length ? <p className="result-empty">{run.lanes?.price === 'pending' ? '尚未开始核价。页面顶部的“查找已成交项目”可以检索这台仪器的历史成交记录。' : run.lanes?.price === 'running' ? '正在查找成交公告并核对型号、金额与配置，核实后会显示在这里。' : `本轮检索没有取得可采信的成交记录：没有一条金额同时满足上面列出的收录标准。${run.price_assessment ? '因此也不能据此说这台机器在别处没有成交记录；这份报价是否合理，见下面「分析结论」。' : '所以既不能判断这份报价是否合理，也不能据此说这台机器在别处没有成交记录。'}`}</p> : ordered.map((price, i) => <PriceRecord key={`${price.url || price.model || 'price'}-${i}`} price={price} tender={run.tender_requirements} position={{index: i + 1, total: ordered.length}} />)}
      <PriceRejectionSection rejections={run.price_rejections} targetModel={baseline.model} adoptedCount={prices.length} />
      {run.historical_summary?.map((s,i) => <p key={i}>{s.count} 条同口径独立成交记录，中位数 {s.median.toLocaleString('zh-CN')} {s.currency}，仅作历史参考。</p>)}
    </section>}
    {/* 本轮没有官方成交记录时，网页上的金额按"离成交价有多近"排序正常列出——那时它是
        这台机器唯一的交易信号。一旦有官方成交记录，``marketReferenceBlock`` 返回 null，
        这一块整个不渲染：那种情况下这些页面属于"历史招标公告"之外的商业线索，前端连
        域名和字样都不出现（用户口径）。 */}
    {section !== 'alternatives' && <MarketReferenceSection leads={leads} values={run.prices || []} />}
    {/* 核价页原来把同一批线索印了两遍：这里一行「待核实价格线索」，
        「市场参考价」再逐条印一遍。两处内容就是同一批 ``run.price_leads``，所以下面
        这一行只在**没有官方成交记录、因而市场参考区块也列出同一批线索**时才需要——
        有官方记录时它本来就是重复的。 */}
    {section !== 'alternatives' && leads.length > 0 && !prices.length && <section className="result-section price-leads-section" aria-label="待核实价格线索"><div className="result-section-heading"><div><h3>待核实价格线索</h3><p>这些金额来自网页挂牌、资产登记或尚未确认的公示内容，未进入正式成交价比较。同一型号只占一行，展开可见每一条来源。</p></div><span>{groups.length} 个型号 · {leads.length} 条</span></div>{groups.map(group => <PriceLead key={group.model} group={group} />)}</section>}
    {section !== 'price' && <section className="result-section" aria-label="跨品牌比较"><h3>跨品牌比较</h3><p>已检索到 {cross.length} 款、来自 {brands.length} 个其他品牌。达到70%已核验相似度的候选进入可比较列表；差异和缺证仍需单独确认。</p>
      {!cross.length && <p className="result-empty">{run.lanes?.alternatives === 'pending' ? '尚未开始比较。点击“查找并比较其他品牌”，按已确认的实验要求寻找候选产品。' : run.lanes?.alternatives === 'running' ? '正在查找其他品牌，并逐项核对实验要求。' : '本轮尚未取得其他品牌的有效候选，不能据此认定市场上没有替代品。'}</p>}
      {/* 查价出错时只在这一个区块里说一句：这是"这一次查价没成"，与"这个候选
          没有价格"是两件事，不能混进卡片的价格结论里。 */}
      {lookupError && <p className="result-note" role="status">{lookupError}</p>}
      {brands.map(brand => { const group = cross.filter(a => brandGroupKey(a) === brand); return <div className="brand-group" key={brand}><Alternative candidate={group[0]} baseline={baseline} sources={sources} officialPrices={run.prices || []} {...priceCards} />{group.length > 1 && <details><summary>{group[0].brand} 的另外 {group.length - 1} 个型号（仍算同一个品牌）</summary>{group.slice(1).map((a,i) => <Alternative key={i} candidate={a} baseline={baseline} sources={sources} officialPrices={run.prices || []} {...priceCards} />)}</details>}</div> })}
      {same.length > 0 && <details><summary>原品牌其他型号（{same.length} 款，仅供参考）</summary>{same.map((a,i) => <Alternative key={i} candidate={a} baseline={baseline} sources={sources} reference officialPrices={run.prices || []} {...priceCards} />)}</details>}
      {unknown.length > 0 && <details><summary>品牌信息不完整的线索（{unknown.length} 款）</summary><p>品牌没有核实，暂不计入跨品牌比较。</p>{unknown.map((a,i) => <Alternative key={i} candidate={a} baseline={baseline} sources={sources} officialPrices={run.prices || []} {...priceCards} />)}</details>}
      {run.reference_candidates?.length > 0 && <details><summary>未进入深入核验的其他线索（{run.reference_candidates.length} 条）</summary><p className="result-note">这些候选没有走进深入核验，卡片上只展示已有的线索与来源；要查其中某一条的价格，点它自己的“查这个的价格”。</p>{run.reference_candidates.map((a,i) => <div className="reference-candidate" key={a.candidate_id || i}><p className="result-note">{REFERENCE_REASON_LABELS[a.reference_reason] || '尚未深入核验。'}</p><Alternative candidate={a} baseline={baseline} sources={sources} officialPrices={run.prices || []} reference={sameBrandAsBaseline(a, baseline)} {...priceCards} /></div>)}</details>}
    </section>}
    <section className="result-section" aria-label="检索来源"><h3>检索来源与未完成事项</h3><p>被搜索到不代表已取得正文或价格。下面按来源身份分组，普通商家页面不能当作厂家或成交依据。</p>
      <details><summary>查看来源清单（{sources.length} 个）</summary><label className="source-filter">筛选来源<select value={sourceFilter} onChange={e => setSourceFilter(e.target.value)}><option value="all">全部来源</option><option value="procurement">采购公告线索</option><option value="manufacturer">厂家参数资料</option><option value="unread">未取得正文</option></select></label>
        {/* error_reason 里嵌着后端拼进去的失败代号（「该来源连续两次无法读取（empty_body），
            其他来源可正常访问；保留URL及原始失败原因待补查」），原来这里是原样渲染，一个翻译层
            都不过 —— 全量实测 417 条会渲染的 error_reason 里有 43 条带代号。走 publicText 做
            句内替换：只把代号换成中文，句子其余部分（「可以尝试手动打开来源」这类有用信息）
            一个字符都不动。别改成 researchIssue(...).message —— 那是给列表项生成整句话的，
            会把整句换掉、把有用信息丢掉。历史上已经存下来的 run 改不掉了，所以就算后端源头
            改成直接输出中文，这一层也仍然要留着。 */}
        {filtered.length ? <>{grouped.groups.map(group => {
          // 「产品页与门户线索（非官方来源）」这一组的标题与说明本身就是"这一类来源
          // 是什么"，有官方成交记录、这一组又没有一行时会显得像还剩一批非官方来源；
          // 没有行就不渲染它（有行时照常渲染，见 tests/sourceGrouping.test.mjs）。
          if (!group.rows.length) return null
          return <div className="source-group" key={group.key}>
            <h4>{group.title}（{group.rows.length} 个）</h4>
            <p className="source-group-note">{group.note}</p>
            {group.rows.map(s => <article key={s.url} className="source-row"><SourceLink url={s.url}>{s.title || siteNameOf(s.url)}</SourceLink><p>{sourceLabels[s.source_type] || '来源类型未核实'} · {s.body_status === 'fetched' ? '已读取正文' : s.body_status === 'unavailable' ? '来源不可读取，可尝试手动打开' : '未取得正文'}</p>{s.purpose === 'tender' && s.source_type !== 'tender_document' && <p>采购检索线索，公告身份尚未核实。</p>}{s.body_status === 'fetched' && s.body_text && <SourceBody source={s} />}{s.body_status !== 'fetched' && s.error_reason && <p>{publicText(s.error_reason, {})}</p>}</article>)}
          </div>
        })}
        {/* 分组只是换个排法，不能吃掉来源。分成几组、加起来必须等于筛选后的条数。 */}
        {grouped.classified !== grouped.total && <p className="source-group-note">还有 {grouped.total - grouped.classified} 个来源的类型未核实，未列入上面任何一组，请按 URL 自行核对。</p>}
        </> : <p>本轮没有此类来源。</p>}
      </details>
      <details><summary>为什么还不能给出采购建议</summary><p>价格资料：{laneLabels[run.lanes?.price] || '尚未核实'}；其他品牌资料：{laneLabels[run.lanes?.alternatives] || '尚未核实'}。</p><ResearchIssues values={[...(run.gaps || []), ...(run.message ? [run.message] : [])]} sources={sources} visibleUrls={visibleUrls} /></details>
    </section>
  </>
}

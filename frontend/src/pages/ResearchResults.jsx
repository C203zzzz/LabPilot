import React, { useState } from 'react'
import SourceLink, { safeWebUrl } from '../components/SourceLink'
import { ActionButton } from '../components/ui'
import ResearchIssues from '../components/ResearchIssues'
import { displayPriceValue as text, formatPriceAmount, hasDisplayValue, hasProcurementUnit, MARKET_REFERENCE_FIELD_LEGEND, marketReferenceBadge, marketReferenceBlock, MARKET_REFERENCE_TIERS, orderedTransactionPrices, priceLeadGroups, priceLeadsForSection, priceRangeText, priceSourceLinks, procurementUnit, productSpec, transactionPrices } from '../services/pricePresentation'
import { siteNameOf, sourceSummary } from '../services/siteNames'
import { splitTenderClauses, tenderDeviceForModel, tenderSource } from '../services/tenderPresentation'
import { alternativePresentation, candidateTitle, configListing, sameBrandAsBaseline } from '../services/alternativePresentation'
import { applyPriceLookup, beginLookup, endLookup, priceButtonState, priceLookupView } from '../services/alternativePriceLookup'
import { candidateSourceEntries } from '../services/candidateSources'
import { configurationModelLine, configurationModelNotice } from '../services/configurationModel'
import { publicText } from '../services/publicText'
import { productSpecLayout } from '../services/productSpecLayout'
import { groupSources } from '../services/sourceGrouping'
import { normalizeModel, partitionRejections, rejectionDetail, rejectionGroupNote, rejectionOwnerLabel, rejectionSectionCopy } from '../services/rejectionPlacement'
import { priceEmptyDisclosure } from '../services/priceEmptyDisclosure'
import { supplierQuestionSheet } from '../services/supplierQuestionSheet'
import { candidateFacts, candidateLists, rosterEntries, watchedAnchors } from '../services/candidateRoster'

const labels = {historical_configuration:'同型号历史成交', historical_model:'同型号日期价格参考', model_reference:'同型号历史成交参考'}
const sourceLabels = {manufacturer_website:'厂家官网', manufacturer_download:'厂家资料', tender_document:'采购公告 / 合同', dealer:'经销商', aggregator:'仪器门户 / 转载', unknown:'发布者未核实'}
const laneLabels = {completed:'本轮检索结束', failed:'检索中断', pending:'尚未执行', running:'正在检索'}
//: 规范项（2026-10-07）：候选卡片不再输出「满足／差异／未知」三态。原来那个把后端
//: status 映射成"满足／不满足／缺少证据"的常量随之下线——页面上一处都不再印这类判断。
//: 迭代规范（2026-10-07）：候选清单合并成一份之后，原来那张"为什么它落在备用清单里"的
//: 原因文案表（``reference_reason``）整块下线——不再有两级清单，就不再解释层级。

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
 * 它是**一张价格记录卡里的第二块**（业务口径：型号价格和配置是同级的，同在一张
 * 卡里），与第一块、和后面的「来源」共处一张卡；自成一块靠底色与左边框区分，
 * 不靠"另开一张卡"。结构化交给 ``productSpecLayout``——那段 ``configuration`` 是
 * 整页正文（参数 + 同一张表按行重复 + 尾部"相关推荐"的产品名和裸链接），原来用
 * ``<pre>`` 原样铺出来就是用户说的"这个配置清单太丑了"。现在按分组出网格、
 * 多列表出真表格，解析不出来的行不铺开；表格的列间隔与数值列右对齐在
 * ``styles/index.css`` 的 ``.product-spec-table`` 那组规则里，数值列由
 * ``productSpecLayout`` 逐列标出来（``table.numeric``，按数据判、不按表头猜）。
 *
 * **空壳整块不渲染**（设计规范，业务口径"要改"）：``productSpec()``
 * 有值（有 ``configuration`` 文本）但 ``productSpecLayout`` 一个参数组都分不出来时，
 * 原来会渲染成标题 + 一句口径说明 + 「厂家资料正文里没有可对齐的参数条目，请打开原文
 * 核对。」三行，一个参数都没有，对用户就是噪音。现在这种情况**整块不渲染**
 * （不是收成一行、也不是只留标题）；有参数组时照旧，那句口径说明（标称参数 ≠ 成交
 * 配置）还在。价格记录卡的配置来源链接不受影响——``PriceSources`` 照样把配置来源印在
 * 卡尾（默认折叠），要核对原文随时点得开。
 * 全部 62 轮台账里 150 条渲染了这一块，其中空壳 **64 条**（43%）；59 轮口径是
 * 120 条里空壳 44 条。
 */
function ProductSpecCard({ spec }) {
  const layout = productSpecLayout(spec.text)
  // 说明句里原来内联着「查看{label}原文」的链接。现在配置来源的链接统一放在
  // 卡尾那块「来源」里（业务口径：来源默认折叠、展开后给出价格来源与配置来源
  // 两条），同一句话里再挂一遍就是同一个地址印两次。
  // 迭代规范（2026-10）：用户说这句小字太啰嗦。原来是三重否定逐条列（不是成交记录里
  // 写的配置／不是政府采购公告里写的配置／不是招标文件对投标产品提出的门槛），
  // 收成一句「上的标称参数，不是成交配置。」——**关键区分（标称参数 ≠ 成交配置）
  // 一个字没丢**，去掉的只是同一件事换三种说法的重复。这句话现在只有价格记录卡
  // 这一处在用（候选卡上那一块 2026-10-07 下线了），所以不写"这条成交"这种只在
  // 其中一处成立的指代。出处里的站点名换成中文（原来印的是
  // ``www.xiangyilxj.com`` 这种英文域名）。
  // 空壳（一个参数组都分不出来）整块不渲染：标题 + 口径说明 + 「没有可对齐的参数条目」
  // 三行没有一个参数，对用户是噪音（见上面的说明）。
  if (!layout.groups.length) return null
  const itemCount = layout.groups.reduce((sum, group) => sum + group.rows.length + (group.table?.rows?.length ?? 0), 0)
  return <details className="product-spec-card" aria-label="该型号产品资料的标称参数（非成交配置）">
    <summary className="product-spec-summary">
      <span>▸ 查看同型号标称参考参数（{itemCount} 项 · 非成交配置）</span>
    </summary>
    <div className="product-spec-body">
      <h4>该型号产品资料的标称参数（非成交配置）</h4>
      <p className="tender-origin">{spec.label}{spec.url ? `（${siteNameOf(spec.url)}）` : ''}上的标称参数，不是成交配置。</p>
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
    </div>
  </details>
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
 *    产品页」），不是 URL、也不是英文域名——业务口径：不要把链接本身展示出来；
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
  //
  // **候选卡片上那一处同名块已经下线（2026-10-07），这一处保留**：候选卡片上有
  // 「该候选页面写到的配置」摆着同一份 ``configuration`` 文本，这一块在那里是重复的；
  // 成交价这一行没有第二个出口——下面 ``terms`` 的过滤条件会把 ``configuration``
  // 那一行压掉（不压的话就会出现"产品参数块 + 配置格"两份），所以删掉这一块等于
  // 把这条成交的配置从界面上抹掉。59 轮台账实测：122 条成交价记录里 120 条渲染了
  // 这一块（76 条有参数表、44 条是"页面上没有可对齐的参数条目"的空壳）。
  const spec = productSpec(price)
  // 配置统一看招标文件；只有招标文件里没有这台设备时，才回落到公告自己写的配置。
  const fromTender = Boolean(tenderDeviceForModel(tender, price.model))
  const terms = [['configuration','配置'],['tax','税费'],['shipping','运费'],['installation','安装培训'],['warranty','保修'],['service_response','报修响应'],['service_onsite','上门处理'],['delivery','交付']]
    .filter(([key]) => hasDisplayValue(price[key]) && !(key === 'configuration' && (fromTender || spec)))
  // 卡内三块的排法（业务口径：型号价格和配置是**同级的**，同在一张卡里）：
  //   第一块 型号与价格并排一行（原来型号单独占一行、在价格下面）；
  //   第二块 配置——有产品资料时是 ``ProductSpecCard``，没有时是配置格与
  //          ``TenderConfig``（两者互斥，见 ``productSpec`` 那段的说明）；它仍在
  //          这里、和第一块同一张卡里，不再是并列的第二张卡；
  //   第三块（卡尾）「来源」，默认折叠，展开后给出价格来源与配置来源两条链接。
  // 同一个型号命中多条成交记录时，每张卡右上角给一个「第 n 条 / 共 m 条」——
  // 编号是调用方（``position``）算好的：只有一条时不印（一条记录谈不上第几条），
  // 区块标题 「可比历史成交记录」 在 ``prices.map`` 外面、只渲染一次，这里绝不
  // 再加一遍标题（需避免重复展示「又重复一次可比历史成交记录」）。
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
      {/* 迭代规范（2026-10）：用户说这串小字重复——缺了哪些商务条件，下面那张逐项字段表
          已经一条一条列出来了，这里再串一遍就是同一件事印两遍。**「不是当前报价」
          这一句事实一个字没丢**（把历史成交读成当前报价是这个页面上最容易犯的错，
          这句就是拦它的）。 */}
      <p className="price-boundary">这是同型号历史成交记录，不是当前报价。{!gaps.length && '供货条件需重新确认。'}</p>
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
 * 「认出」 step drops a model-mismatched award (for example 中科中佳/HC-3016R at
 * ￥32,980 when the target is H1850R) and the user only saw 0 条. These reasons
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

function PriceRejectionSection({ rejections, role = 'price', model = '', adoptedCount = 0 }) {
  const list = rejections || []
  if (!list.length) return null
  // 分节：成交价那一节与候选品卡片各自调用它，标题、说明与型号
  // 称呼都按归位后的身份说（``rejectionSectionCopy`` / ``rejectionOwnerLabel``），
  // 不再把候选型号叫成"本次目标型号"。
  const copy = rejectionSectionCopy(role, { model })
  const grouped = list.reduce((acc, item) => {
    const code = item.reason_code || 'unexplained'
    acc[code] = acc[code] || []
    acc[code].push(item)
    return acc
  }, {})
  return <details className="price-rejection-section">
    <summary>{copy.title}（{list.length} 条）</summary>
    <p>{copy.lead}{adoptedCount ? '' : '列出这些记录是为了说明“为什么是 0 条”。'}</p>
    {Object.entries(grouped).map(([code, items]) => {
      // 共同解释一组说一次（迭代规范，设计规范）：原来每条记录都把
      // "同品类的不同型号不能互相认定成交价。"重复一遍，实测一节里重复 12 次。
      const note = rejectionGroupNote(code, { role, model: String(items[0]?.model || model || '').trim() })
      return <div className="rejection-group" key={code}>
        <h4>{rejectionLabels[code] || '其他原因（未分类）'}（{items.length} 条）</h4>
        {note && <p className="rejection-group-note">{note}</p>}
        {items.map((item, index) => <article className="price-lead" key={`${item.url}-${index}`}>
          <div className="price-lead-heading">
            <div>
              {/* 行首只留型号称呼：原因已经在组标题上写过一次，逐条不再重复
                  （迭代规范 去重复；``引文中出现的型号`` 那一行也删了——detail 里已经带着它）。 */}
              <strong>{rejectionOwnerLabel(item, { role })}</strong>
            </div>
            <SourceLink url={item.url}>查看被拒来源</SourceLink>
          </div>
          <p>{rejectionDetail(item, { role })}</p>
          {item.snapshot_sha256 && <p className="price-boundary">抓取时的页面快照已留存；校验值在随报告导出的来源清单里。</p>}
          {item.excerpt && <details><summary>查看被拒原文</summary><blockquote>{item.excerpt}</blockquote></details>}
        </article>)}
      </div>
    })}
  </details>
}

/**
 * 「向销售索要的条件」话术。
 *
 * 内容全部来自本轮真实缺口（``supplierQuestionSheet`` 是纯函数，node --test 直接
 * 断言）：这里只负责摆出来 + 一个"复制"按钮。话术用 ``<pre>`` 原样展示——换行与编号
 * 是它能直接粘进邮件/聊天窗口的前提。剪贴板 API 在非安全上下文或旧浏览器上会抛错，
 * 所以失败时给出可操作的替代说法，不静默什么都不发生。
 */
function SupplierQuestionSheet({ sheet }) {
  const [copyState, setCopyState] = useState('')
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(sheet.body)
      setCopyState('已复制')
    } catch {
      setCopyState('复制失败，请手动选中上面的文字')
    }
  }
  return <section className="result-section supplier-question-section" aria-label="向销售索要的条件">
    <h3>向销售索要的条件（可直接复制）</h3>
    <p>{sheet.missingLabels.length
      ? `本轮资料里没有写清的项：${sheet.missingLabels.join('、')}。下面这段可以直接复制发给销售。`
      : '本轮销售报价里这些条件都提到过；发送前建议按同样口径再书面确认一遍。'}</p>
    <pre className="supplier-question-body">{sheet.body}</pre>
    <ActionButton variant="secondary" onClick={copy}>{copyState || '复制这段话术'}</ActionButton>
  </section>
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

//: 规范项（2026-10-07）：候选卡片不再输出判定标签，原来那套 verdict 色调映射
//: （success/warning/danger/muted → status-tag 类名）没有调用点了，随之下线。

/**
 * 一张替代候选卡片（两段式：先展示候选，用户点「查这个的价格」才查这一个）。
 *
 * 卡片上每一块的数据来源都是后端已经写好的字段，前端不判读、不补数：
 * - 型号与品牌 → ``candidate.model`` / ``candidate.brand``；品牌没解析出来时印
 *   ``品牌未解析``（``candidateTitle``），**不留空**；
 * - 配置 → ``candidate.config_match.items`` 里**逐字读到**的那些（``configListing``），
 *   只摆"配置项 + 页面上写到的 + 原文出处"；没读到的项只报个数，不铺一排"未知"
 *   ；
 * - 证据质量 → ``evidenceNotes``：原文互相冲突、来源尚未核实两类，各自单独成组；
 * - 原文行 → ``candidate.excerpt``（发现这一条时读到的那句原文）与逐项证据的
 *   ``observed.evidence.excerpt`` + 其 ``url``；
 * - 价格 → 候选自己的 ``price_lookup``（后端 ``alternative_price_lookup``：没查／
 *   查了没有／有来源但没被采信三态分开说）；点过按钮之后改用这一次响应的结果。
 *
 * 「查这个的价格」按钮的状态与点击逻辑都在 ``services/alternativePriceLookup.js``
 * 里（纯函数，node --test 直接断言）。**这一张卡的查价只影响这一张卡**：结果按
 * ``candidate_id`` 写进 ``lookupResults``，别的卡片收到的 props 一个字没变。
 */
function ConfigBlockFold({ value }) {
  const lines = String(value || '').split(/\r?\n/)
  const preview = lines.slice(0, 5).join('\n').slice(0, 220)
  return <div className="config-source-summary-wrap">
    <pre className="config-source-text config-source-preview">{preview}{lines.length > 5 || String(value || '').length > 220 ? '…' : ''}</pre>
    <details className="config-source-fold">
      <summary>展开完整原文（{lines.length} 行）</summary>
      <pre className="config-source-text">{value}</pre>
    </details>
  </div>
}

function Alternative({ candidate, baseline, sources, reference = false, officialPrices = [], lookupResults = {}, pendingLookups = {}, onLookupPrice, rejectionsByModel = {}, anchors = [] }) {
  // 这个候选品自己那份核价记录里被拒的金额：按记录自带的 ``model`` 归位，
  // 每张卡只拿自己那几条。归属判据在 ``services/rejectionPlacement.js`` 里。
  const rejections = rejectionsByModel[normalizeModel(candidate.model)] || []
  // 判据与核价页、报告、来源清单同一个：**这个任务**有没有官方成交记录
  // （``run.prices`` 里的 award/contract），不是这个候选自己的 ``candidate.prices``
  // ——候选的价格依据常常只有挂牌价，用它当判据会让同一页上两处结论相反
  // （核价页判定"有官方记录"，候选卡片却把线索列出来）。
  const block = marketReferenceBlock(candidate.price_leads || [], officialPrices)
  const { verdict, evidenceNotes, provisionalNote, category } = alternativePresentation(candidate, { baseline, reference })
  // 配置那一栏带上用户关注参数的锚点：命中的项在页面上加粗（只加粗，不上色、不打勾）。
  const listing = configListing(candidate, { anchors })
  // 候选自己那份标称参数（``ProductSpecCard``）在候选卡片上**已经下线**（2026-10-07
  // 设计规范：「没有配置就不会作为候选展示了，直接保留候选配置即可」）。理由两条，
  // 都用 59 轮台账（``output/e2e-final/runs``，排除 ``merge-audit-hve50-r2`` /
  // ``m5-ui-check`` / ``final-check``）实测量过：
  //   * 182 条候选渲染过这一块，**182 条全都有**下面「该候选页面写到的配置」的内容
  //     （``configListing().rows`` 非空）——这一块是重复的；
  //   * 这份配置文本就是 ``candidate.configuration``，而 ``configListing`` 已经把它
  //     当成「配置原文」一项摆出来了，删掉之后**没有一张候选卡片变成空的**
  //     （「两者都没有、靠这一块撑着」的候选 = 0 条）。
  // 价格记录卡（``PriceRecord``）上那一处不在这条口径里，见那里的说明。
  // 「配置所属型号」（设计规范）：后端 迭代规范 已经算好 ``configuration_model``
  // （判不出是空串），这一层只决定要不要说、怎么说——空串不说、与候选型号相同不说、
  // 不同时把两个写法都留下（候选型号是查价的键与来源痕迹，抹不掉）。判据在
  // ``services/configurationModel.js``。
  const configurationModel = configurationModelNotice(candidate)
  // 候选定位键：接口按 ``candidate_id`` 查价（``sha256(identity(model)+url)[:16]``）。
  // **不退回数组下标**——候选名单一变，下标就指到别人头上；没有编号的老数据宁可不给按钮。
  const candidateId = String(candidate.candidate_id || '').trim()
  const lookup = candidateId ? lookupResults[candidateId] : null
  const priceView = priceLookupView(candidate, lookup)
  // 事实标记（设计规范）：查没查过价、做没做过逐项比对。合并成一份清单之后
  // 这两件事更要说清楚——它们是事实，不是判断，不许一起抹平。
  const facts = candidateFacts(candidate, { lookup })
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
      </div>
      {verdict.detail && <p className="verdict-detail">{verdict.detail}</p>}
    </div>
    <p className="alternative-category">品类：{category}</p>
    <p className="alternative-facts-line">{facts.line}</p>
    {/* 这份配置属于哪个型号：只在**与候选自己的型号不同**时出现，两个写法都留下
        （``设计规范`` §1.1 的 ``bekomat30`` → ``BBS-V600`` 就是这一条）。空串或与
        候选型号相同都不印——不猜、不重复、不加噪音。 */}
    {configurationModel && <p className="alternative-configuration-model">{configurationModelLine(configurationModel)}</p>}
    {evidenceNotes.length > 0 && <dl className="alternative-facts">{evidenceNotes.map(group => <div className={`alternative-fact fact-${group.tone}`} key={group.key}>
      <dt>{group.label}</dt>
      <dd>{group.items.map(item => <span className="fact-item" key={item.name}>{item.name}{item.note && <small>{item.note}</small>}</span>)}{group.note && <small className="fact-note">{group.note}</small>}</dd>
    </div>)}</dl>}
    {/* 配置那一栏：只摆这条候选自己的页面/资料里**逐字读到**的
        配置——指标名 + 页面里写的写法 + 原文出处。没有"已确认要求"那一列、不判
        "一致／不同／未知"、不给比例与接近程度分数：判断满足与否是采购员按
        原文自己做的事（口径见 ``alternativePresentation.js`` 模块开头与
        ``回归基线`` 的 规范项）。 */}
    {listing.present && <section className="alternative-config-match" aria-label="这条候选页面上读到的配置">
      <h5>该候选页面写到的配置{listing.readCount ? `（${listing.readCount} 项）` : ''}</h5>
      <p className="config-match-legend">{listing.boundary}</p>
      {listing.emptyNote && <p className="result-note">{listing.emptyNote}</p>}
      {listing.readCount > 0 && listing.missingCount > 0 && <p className="result-note">另有 {listing.missingCount} 项该页面未写到，未列入下表。</p>}
      {listing.readCount > 0 && <div className="comparison-scroll" tabIndex={0} role="region" aria-label={`${candidate.model || '候选'}页面上读到的配置`}>
        <table className="config-match-table">
          <thead><tr><th>配置项</th><th>页面上写到的</th><th>原文</th></tr></thead>
          <tbody>{listing.rows.map(row => {
            const longBlock = row.block && (String(row.value || '').split(/\r?\n/).length > 5 || String(row.value || '').length > 220)
            return <tr key={row.key} className="config-match-row">
              <th scope="row" className={row.watched ? 'is-watched' : undefined}>{row.name}</th>
              <td>{longBlock ? <ConfigBlockFold value={row.value} /> : <>{row.block ? <pre className="config-source-text">{row.value}</pre> : row.value}</>}</td>
              <td>
                {row.url && <SourceLink url={row.url}>这一项的原文</SourceLink>}
                {row.excerpt && <blockquote>{row.excerpt}</blockquote>}
              </td>
            </tr>
          })}</tbody>
        </table>
      </div>}
    </section>}
    {provisionalNote && <p className="result-note">{provisionalNote}</p>}
    {candidate.excerpt && <blockquote className="alternative-excerpt">{candidate.excerpt}</blockquote>}
    <CandidateSources candidate={candidate} />
    <ModelExcerpt candidate={candidate} sources={sources} />
    {/* 原来这里还有一张折叠的逐项参数对照表（指标 / 基准记录的要求 / 资料里查到的 /
        判断与依据）。规范项（2026-10-07）之后它整块下线：资料里查到的那些值已经并进上面的
        「该候选页面上读到的配置」（``configSourceRows`` 的第二个来源就是 ``matrix``），
        而"基准记录的要求"与"判断"这两列正是用户要求不再输出的比较与结论。证据本身没有
        丢——每一条都带着原文与出处，冲突与临时来源由上面的证据质量那一行单独说。 */}
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
        见 设计规范 与 backend/services/alternative_price_lookup.py 模块开头）；
        按钮的 disabled 与请求形状由 ``priceButtonState`` / ``lookupCandidatePrice``
        负责，点击只 POST 路径里那一个 ``candidate_id``。 */}
    <section className="alternative-price alternative-price-compact" aria-label={`${candidate.model || '这个候选'}的价格`}>
      <div className="alternative-price-inline">
        <div className="alternative-price-status-wrap">
          <span className={`alternative-price-status price-${priceView.tone}`} role="status">{priceView.label}</span>
          {priceView.note && <span className="alternative-price-note">{priceView.note}</span>}
          {!evidence.length && <span className="alternative-price-note">没有已核实的单价，暂不能比较采购成本。</span>}
        </div>
        <ActionButton variant="secondary" icon={null} disabled={button.disabled} onClick={() => { if (onLookupPrice) onLookupPrice(candidateId) }}>{button.label}</ActionButton>
      </div>
      {button.hint && <p className="action-hint">{button.hint}</p>}
      {evidence.length > 0 && <details><summary>该型号价格依据（{evidence.length} 条）</summary>{evidence.map((p,i) => <PriceRecord key={`${p.url || p.model || 'price'}-${i}`} price={p} position={{index: i + 1, total: evidence.length}} />)}</details>}
    </section>
    {/* 这个候选品自己那份核价记录里被拒的金额：从前它们和成交价
        链路的记录堆在同一节，报告里就出现了「与本次目标型号 LS-50LD 不一致」这种
        串链路的句子——LS-50LD 是候选品，采购目标是另一个型号。现在按记录自带的
        ``model`` 归位到这张卡里，措辞也按候选品的身份说。 */}
    {rejections.length > 0 && <PriceRejectionSection rejections={rejections} role="candidate" model={candidate.model} />}
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
  // 候选清单（设计规范）：**不再分两级候选**——两份名单拼成一份，
  // 底层结构一个字没动（``candidate_id``、查价、撤除都挂在原来那两份名单上）。
  // 排序只按用户关心的事实：一级「与关注参数对上的项数」，二级「配置项总数」，同分稳定。
  // 判据与加粗规则都在 ``services/candidateRoster.js``（纯函数，node --test 直接断言）。
  const anchors = watchedAnchors(baseline?.parameters)
  // 数据来源：后端有合并清单（``run.candidates``）就用它，没有就回退两份名单拼接
  // （迭代规范 提交四；两条路的候选与顺序逐字一致，见 ``candidateRoster.candidateLists``）。
  const roster = rosterEntries(...candidateLists(run), {
    isReference: candidate => sameBrandAsBaseline(candidate, baseline),
    anchors,
    configCount: candidate => configListing(candidate, { anchors }).rows.filter(row => !row.block).length,
    watchedCount: candidate => configListing(candidate, { anchors }).watchedCount,
  })
  const leads = priceLeadsForSection(run, alternatives, section)
  const groups = priceLeadGroups(leads)
  // 有官方成交记录时，来源清单只留后端判定为权威来源的那些（``authority`` 是
  // ``official_channel`` 或 ``manufacturer``：官方历史招标公告及其发布机构、机构自有
  // 官网、厂家资料）；没有官方记录时全部列出，因为那时商业线索是唯一的参考。
  // 判据由后端 ``source_authority`` 按已有分类给出，前端不按域名猜、也不手工维护
  // 域名表。``groupSources`` 拿同一个字段分组，两处不会各说一套。
  // 规范项（2026-10-07）：拒收记录按记录自带的 ``model`` 归位——等于比较基准型号的进
  // 成交价模块，等于某个候选型号的进那张候选卡。判据全在 ``rejectionPlacement`` 里
  // （纯函数，node --test 直接断言），这里只用它的结果。
  const candidateModels = candidateLists(run).flat().map(item => item.model)
  const rejections = partitionRejections(run.price_rejections, { baselineModel: baseline?.model, candidateModels })
  // 规范项（2026-10-07）：价格栏空着时要交代查了什么、为什么没有。数据全在 run 台账里。
  const priceDisclosure = prices.length ? null : priceEmptyDisclosure(run)
  // 规范项（2026-10-07）：本轮缺口 → 一段可直接发给销售的话术。
  const questionSheet = supplierQuestionSheet({ baseline, prices: run.prices || [] })
  const listedOnly = prices.length > 0
  const sources = [...new Map((run.sources || [])
    .filter(s => s.url && (!listedOnly || (s.authority && s.authority !== 'unlisted')))
    .map(s => [s.url, s])).values()]
  const filtered = sources.filter(s => sourceFilter === 'all' || (sourceFilter === 'procurement' ? s.purpose === 'tender' || s.source_type === 'tender_document' : sourceFilter === 'unread' ? s.body_status !== 'fetched' : ['manufacturer_website','manufacturer_download'].includes(s.source_type)))
  const grouped = groupSources(filtered)
  // 缺口文本里嵌着触发它的页面地址（``run.gaps`` 里就有聚合站的详细页），那一列
  // 说明是后端算出来的事实，一字不改；有官方成交记录时只把不列出的域名挡在渲染外。
  const visibleUrls = listedOnly ? new Set(sources.map(s => s.url)) : null
  // 每张候选卡片都要的那几件（按需查价结果表、进行中表、点击入口，外加 规范项 的
  // 拒收记录分桶）：三张清单（跨品牌/原品牌/品牌不全）传的是**同一份**表——所以点
  // A 卡片只会让 A 的状态变，B 卡片的 props 一个字没动，它的按钮照样能点。
  const priceCards = { lookupResults, pendingLookups, onLookupPrice: lookupPrice, rejectionsByModel: rejections.byModel }
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
      {/* 规范项（2026-10-07）：本轮没有可采信成交价时**不留白**，把"查了什么、为什么
          没有"写清楚——查了几个通道、检索/抓取多少次、哪些通道失败或限流、聚合站
          转载价几条（二手来源）、平台挂牌价几条（非成交价）。数据全在 run 台账里，
          这里不抓任何新东西，也不拿"同品牌同品类成交价"之类替身填空（用户否决）。 */}
      {!prices.length && priceDisclosure && <details className="price-empty-disclosure">
        <summary>▸ 查看检索明细（{priceDisclosure.rows.length} 项记录）</summary>
        <dl className="price-empty-facts">{priceDisclosure.rows.map(row => <div key={row.key}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}</dl>
      </details>}
      <PriceRejectionSection rejections={rejections.price} role="price" adoptedCount={prices.length} />
      {rejections.other.length > 0 && <PriceRejectionSection rejections={rejections.other} role="other" adoptedCount={prices.length} />}
      {rejections.unattributed.length > 0 && <PriceRejectionSection rejections={rejections.unattributed} role="other" adoptedCount={prices.length} />}
      {run.historical_summary?.map((s,i) => <p key={i}>{s.count} 条同口径独立成交记录，中位数 {s.median.toLocaleString('zh-CN')} {s.currency}，仅作历史参考。</p>)}
    </section>}
    {/* 规范项（2026-10-07）：缺口 → 可直接发给销售的话术。内容来自本轮真实缺口
        （销售报价里没说清 + 成交记录里也缺的那几项），纯函数在
        ``services/supplierQuestionSheet.js``。 */}
    {section !== 'alternatives' && <SupplierQuestionSheet sheet={questionSheet} />}
    {/* 本轮没有官方成交记录时，网页上的金额按"离成交价有多近"排序正常列出——那时它是
        这台机器唯一的交易信号。一旦有官方成交记录，``marketReferenceBlock`` 返回 null，
        这一块整个不渲染：那种情况下这些页面属于"历史招标公告"之外的商业线索，前端连
        域名和字样都不出现（业务口径）。 */}
    {section !== 'alternatives' && <MarketReferenceSection leads={leads} values={run.prices || []} />}
    {/* 核价页原来把同一批线索印了两遍：这里一行「待核实价格线索」，
        「市场参考价」再逐条印一遍。两处内容就是同一批 ``run.price_leads``，所以下面
        这一行只在**没有官方成交记录、因而市场参考区块也列出同一批线索**时才需要——
        有官方记录时它本来就是重复的。 */}
    {section !== 'alternatives' && leads.length > 0 && !prices.length && <section className="result-section price-leads-section" aria-label="待核实价格线索"><div className="result-section-heading"><div><h3>待核实价格线索</h3><p>这些金额来自网页挂牌、资产登记或尚未确认的公示内容，未进入正式成交价比较。同一型号只占一行，展开可见每一条来源。</p></div><span>{groups.length} 个型号 · {leads.length} 条</span></div>{groups.map(group => <PriceLead key={group.model} group={group} />)}</section>}
    {section !== 'price' && <section className="result-section" aria-label="候选"><h3>候选（{roster.length} 台）</h3>
      {!roster.length && <p className="result-empty">{run.lanes?.alternatives === 'pending' ? '尚未开始检索。点击“查找并比较其他品牌”，按已确认的实验要求寻找候选产品。' : run.lanes?.alternatives === 'running' ? '正在查找候选，并逐项核对实验要求。' : '本轮尚未取得其他品牌的有效候选，不能据此认定市场上没有替代品。'}</p>}
      {/* 查价出错时只在这一个区块里说一句：这是"这一次查价没成"，与"这个候选
          没有价格"是两件事，不能混进卡片的价格结论里。 */}
      {lookupError && <p className="result-note" role="status">{lookupError}</p>}
      {roster.map((entry, index) => <Alternative key={entry.candidate.candidate_id || index} candidate={entry.candidate} baseline={baseline} sources={sources} officialPrices={run.prices || []} reference={entry.reference} anchors={anchors} {...priceCards} />)}
    </section>}
    <section className="result-section" aria-label="检索来源"><h3>检索来源与未完成事项</h3>{/* 迭代规范（2026-10）：两句并成一句，两个口径都留着——"被搜索到不等于拿到了
        正文或价格"和"普通商家页面不能当作厂家或成交依据"。 */}
      <p>被搜索到不代表已取得正文或价格；普通商家页面不能当作厂家或成交依据。</p>
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
      {/* 迭代规范（2026-10）：用户说这句"说明太啰嗦"。这一格的价值是**列缺口**（下面
          ``ResearchIssues`` 那一串），不是把两条车道的状态再复述一遍，所以压成
          一行：车道状态还在（读者知道这两条线走到哪了），但不再占一段话。 */}
      <details><summary>为什么还不能给出采购建议</summary><p>两条资料线：价格 {laneLabels[run.lanes?.price] || '尚未核实'}，其他品牌 {laneLabels[run.lanes?.alternatives] || '尚未核实'}。</p><ResearchIssues values={[...(run.gaps || []), ...(run.message ? [run.message] : [])]} sources={sources} visibleUrls={visibleUrls} /></details>
    </section>
  </>
}

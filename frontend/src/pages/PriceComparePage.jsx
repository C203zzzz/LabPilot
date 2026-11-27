import React, { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Database, FileSpreadsheet, RefreshCw, Search, ShieldCheck } from 'lucide-react'
import { ActionButton, EmptyState, InlineNotice, LoadingLabel, PageHeading, StatusTag } from '../components/ui'
import { knowledgeApi } from '../services/api'

const formatLocalTime = value => {
  if (!value) return '—'
  const text = String(value).trim()
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text
  const date = new Date(text)
  if (Number.isNaN(date.getTime())) return text
  return date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
}

const formatLocalDate = value => {
  if (!value) return '—'
  const text = String(value).trim()
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text
  const date = new Date(text)
  if (Number.isNaN(date.getTime())) return text
  return date.toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' })
}

const formatMoney = (value, currency = 'CNY') => {
  if (value == null || value === '') return '—'
  const num = Number(value)
  if (!Number.isFinite(num)) return String(value)
  return `${num.toLocaleString('zh-CN', { maximumFractionDigits: 2 })} ${currency === 'CNY' ? '元' : currency}`
}

const SOURCE_LABELS = {
  supplier_quote: '销售报价',
  research_price: '本轮核实成交价',
  memory_price: '历史记忆成交价',
  price_memory: '历史记忆库',
  alternative_price: '替代型号参考价',
  supplier_reply: '销售回复补充',
  xlyg_award: '小零易购成交库',
}

export default function PriceComparePage() {
  const [searchParams] = useSearchParams()
  const [summary, setSummary] = useState(null)
  const [filters, setFilters] = useState(() => ({
    model: searchParams.get('model') || '',
    brand: searchParams.get('brand') || '',
    category: searchParams.get('category') || '',
    supplier: searchParams.get('supplier') || '',
    keyword: searchParams.get('keyword') || '',
    exact_model: ['1', 'true'].includes((searchParams.get('exact') || '').toLowerCase()),
    include_xlyg: true,
  }))
  const [tab, setTab] = useState('compare')
  const [compareData, setCompareData] = useState({ items: [], stats: null, total: 0 })
  const [quotesData, setQuotesData] = useState({ items: [], stats: null, total: 0 })
  const [docsData, setDocsData] = useState({ items: [], total: 0, elapsed_ms: 0 })
  const [expanded, setExpanded] = useState({})
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function loadAll(currentFilters = filters) {
    setLoading(true)
    setError('')
    try {
      const [sumRes, cmpRes, quoteRes, docRes] = await Promise.all([
        knowledgeApi.summary(),
        knowledgeApi.compare(currentFilters),
        knowledgeApi.quotes(currentFilters),
        knowledgeApi.documents({
          model: currentFilters.model,
          brand: currentFilters.brand,
          keyword: currentFilters.keyword,
        }),
      ])
      setSummary(sumRes)
      setCompareData(cmpRes)
      setQuotesData(quoteRes)
      setDocsData(docRes)
    } catch (err) {
      setError(err.message || '读取历史价格库失败。')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    const nextFilters = {
      model: searchParams.get('model') || '',
      brand: searchParams.get('brand') || '',
      category: searchParams.get('category') || '',
      supplier: searchParams.get('supplier') || '',
      keyword: searchParams.get('keyword') || '',
      exact_model: ['1', 'true'].includes((searchParams.get('exact') || '').toLowerCase()),
      include_xlyg: true,
    }
    setFilters(nextFilters)
    loadAll(nextFilters)
  }, [searchParams])

  async function handleReindex() {
    setSyncing(true)
    setError('')
    setNotice('')
    try {
      const res = await knowledgeApi.reindex()
      const synced = res?.synced || {}
      setNotice(`索引重建完成：已扫描 ${synced.tasks_synced || 0} 个任务，收录 ${synced.quotes_indexed || 0} 条报价、${synced.documents_indexed || 0} 份去重资料（跳过重复 ${synced.documents_deduped || 0} 份）。`)
      await loadAll()
    } catch (err) {
      setError(err.message || '重建历史索引失败。')
    } finally {
      setSyncing(false)
    }
  }

  function submitSearch(event) {
    event.preventDefault()
    loadAll(filters)
  }

  const stats = quotesData.stats || compareData.stats

  return <div className="page-canvas compare-page">
    <PageHeading
      title="历史比价与资料库"
      meta={<StatusTag tone="success"><Database size={12} aria-hidden="true" /> {summary ? `${summary.quotes} 条报价 · ${summary.documents} 份去重资料` : '正在加载索引'}</StatusTag>}
    />
    <div className="compare-toolbar">
      <ActionButton variant="secondary" icon={RefreshCw} disabled={syncing || loading} onClick={handleReindex}>
        {syncing ? '正在同步索引' : '同步全部历史任务'}
      </ActionButton>
      {summary?.last_synced_at && <span className="compare-sync-stamp">最近同步：{formatLocalTime(summary.last_synced_at)}</span>}
    </div>
    {error && <InlineNotice tone="error" title="查询未完成">{error}</InlineNotice>}
    {notice && <InlineNotice title="同步结果">{notice}</InlineNotice>}

    <form className="compare-filter-form" onSubmit={submitSearch}>
      <label>仪器型号
        <input value={filters.model} onChange={e => setFilters(f => ({ ...f, model: e.target.value }))} placeholder="例如：SW-CJ-1FD / H1850R" />
      </label>
      <label>品牌
        <input value={filters.brand} onChange={e => setFilters(f => ({ ...f, brand: e.target.value }))} placeholder="例如：苏净安泰 / DRAWELL" />
      </label>
      <label>仪器品类
        <input value={filters.category} onChange={e => setFilters(f => ({ ...f, category: e.target.value }))} placeholder="例如：超净工作台" />
      </label>
      <label>供应商 / 来源
        <input value={filters.supplier} onChange={e => setFilters(f => ({ ...f, supplier: e.target.value }))} placeholder="供应商或采购单位关键词" />
      </label>
      <label>全文关键词
        <input value={filters.keyword} onChange={e => setFilters(f => ({ ...f, keyword: e.target.value }))} placeholder="配置、保修或参数关键词" />
      </label>
      <div className="compare-filter-toggles">
        <label className="compare-check">
          <input type="checkbox" checked={filters.exact_model} onChange={e => setFilters(f => ({ ...f, exact_model: e.target.checked }))} />
          <span>精确型号硬过滤（防止相近型号混入）</span>
        </label>
        <label className="compare-check">
          <input type="checkbox" checked={filters.include_xlyg} onChange={e => setFilters(f => ({ ...f, include_xlyg: e.target.checked }))} />
          <span>联合查询小零易购成交库</span>
        </label>
      </div>
      <div className="compare-filter-actions">
        <ActionButton type="submit" icon={Search} disabled={loading}>查询对比</ActionButton>
        <ActionButton type="button" variant="ghost" disabled={loading} onClick={() => {
          const reset = { model: '', brand: '', category: '', supplier: '', keyword: '', exact_model: false, include_xlyg: true }
          setFilters(reset)
          loadAll(reset)
        }}>清空条件</ActionButton>
      </div>
    </form>

    {stats && <dl className="compare-stats-grid" aria-label="价格分布统计">
      <div><dt>命中报价明细</dt><dd>{quotesData.total} 条（{compareData.total} 组对比）</dd></div>
      <div><dt>最低单价</dt><dd>{formatMoney(stats.min_price)}</dd></div>
      <div><dt>中位单价</dt><dd>{formatMoney(stats.median_price)}</dd></div>
      <div><dt>最高单价</dt><dd>{formatMoney(stats.max_price)}</dd></div>
      <div><dt>SQL 检索耗时</dt><dd>{stats.elapsed_ms ?? 0} ms</dd></div>
    </dl>}

    <div className="compare-tabs" role="tablist">
      <button type="button" role="tab" aria-selected={tab === 'compare'} className={tab === 'compare' ? 'active' : ''} onClick={() => setTab('compare')}>供应商横向对比（{compareData.total} 组）</button>
      <button type="button" role="tab" aria-selected={tab === 'quotes'} className={tab === 'quotes' ? 'active' : ''} onClick={() => setTab('quotes')}>历史价格明细（{quotesData.total} 条）</button>
      <button type="button" role="tab" aria-selected={tab === 'documents'} className={tab === 'documents' ? 'active' : ''} onClick={() => setTab('documents')}>跨任务去重资料（{docsData.total} 份）</button>
    </div>

    {loading ? <LoadingLabel>正在检索历史比价库</LoadingLabel> : tab === 'compare' ? (
      !compareData.items.length ? <EmptyState icon={FileSpreadsheet} title="当前条件暂无横向对比数据">可放宽筛选条件，或先同步已有历史任务报价。</EmptyState> : (
        <div className="compare-table-wrap"><table className="compare-table">
          <thead><tr><th>供应商 / 来源方</th><th>品牌与型号</th><th>样本数</th><th>最低价</th><th>中位价</th><th>最高价</th><th>最新日期</th><th>配置与商务条件</th><th>明细</th></tr></thead>
          <tbody>
            {compareData.items.map((row, idx) => {
              const rowKey = `${row.supplier}-${row.brand}-${row.model}-${idx}`
              const isOpen = Boolean(expanded[rowKey])
              return <React.Fragment key={rowKey}>
                <tr className={isOpen ? 'compare-row-expanded' : ''}>
                  <td><strong>{row.supplier}</strong><div><small>{(row.source_kinds || []).map(k => SOURCE_LABELS[k] || k).join(' / ')}</small></div></td>
                  <td>{[row.brand, row.model].filter(Boolean).join(' ') || '未标明型号'}<div><small>{row.category || ''}</small></div></td>
                  <td>{row.count}</td>
                  <td>{formatMoney(row.min_price)}</td>
                  <td><strong>{formatMoney(row.median_price)}</strong></td>
                  <td>{formatMoney(row.max_price)}</td>
                  <td className="compare-date-cell">{formatLocalDate(row.latest_date)}</td>
                  <td className="compare-terms-cell">{row.terms_summary || '未附商务条款'}</td>
                  <td className="compare-action-cell"><button type="button" className={`text-action compare-expand-btn ${isOpen ? 'is-open' : ''}`} aria-expanded={isOpen} onClick={() => setExpanded(cur => ({ ...cur, [rowKey]: !cur[rowKey] }))}>{isOpen ? '▾ 收起明细' : `▸ 看明细 (${row.count})`}</button></td>
                </tr>
                {isOpen && <tr className="compare-samples-row"><td colSpan={9}>
                  <ul>{(row.samples || []).map((s, si) => <li key={si}>
                    <span>{formatMoney(s.unit_price)} · {formatLocalDate(s.deal_date)} · {SOURCE_LABELS[s.source_kind] || s.source_kind}</span>
                    {s.excerpt && <p>{s.excerpt}</p>}
                  </li>)}</ul>
                </td></tr>}
              </React.Fragment>
            })}
          </tbody>
        </table></div>
      )
    ) : tab === 'quotes' ? (
      !quotesData.items.length ? <EmptyState icon={FileSpreadsheet} title="没有匹配的价格记录">尝试去掉部分限制条件，或关闭精确型号过滤再查。</EmptyState> : (
        <div className="compare-table-wrap"><table className="compare-table">
          <thead><tr><th>品牌 / 型号</th><th>单价</th><th>数量</th><th>供应商 / 来源</th><th>来源类型</th><th>日期</th><th>摘要</th></tr></thead>
          <tbody>
            {quotesData.items.map(item => <tr key={item.quote_key}>
              <td><strong>{[item.brand, item.model].filter(Boolean).join(' ') || '—'}</strong><div><small>{item.category}</small></div></td>
              <td><strong>{formatMoney(item.unit_price, item.currency)}</strong></td>
              <td>{item.quantity ?? '—'}</td>
              <td>{item.supplier || '—'}</td>
              <td><StatusTag tone="muted">{SOURCE_LABELS[item.source_kind] || item.source_kind}</StatusTag></td>
              <td>{item.deal_date || '—'}</td>
              <td className="compare-terms-cell">{item.excerpt || item.configuration || '—'}</td>
            </tr>)}
          </tbody>
        </table></div>
      )
    ) : (
      !docsData.items.length ? <EmptyState icon={ShieldCheck} title="暂未检索到匹配的去重资料">上传并解析产品说明书或报价单后，这里会自动按内容哈希去重归档。</EmptyState> : (
        <div className="compare-doc-list">
          {docsData.items.map(doc => <article className="compare-doc-card" key={doc.content_hash}>
            <div className="compare-doc-head"><strong>{doc.source_name}</strong><StatusTag tone="muted">{doc.char_count} 字</StatusTag></div>
            <p className="compare-doc-meta">型号：{[doc.brand, doc.model].filter(Boolean).join(' ') || '未关联型号'} · 哈希前缀：{doc.content_hash.slice(0, 12)}</p>
            <p className="compare-doc-excerpt">{doc.summary_text}</p>
          </article>)}
        </div>
      )
    )}
  </div>
}

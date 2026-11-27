import React from 'react'
import SourceLink from './SourceLink'
import { groupResearchIssues } from '../services/researchCopy'

/**
 * 缺口与相关来源。``visibleUrls`` 是"这一页允许出现的来源地址"白名单。
 *
 * 缺口文本里常把触发它的页面 URL 一起带出来（``run.gaps`` 里就嵌着聚合站的
 * 详细页地址），而缺口本身是**后端算出来的事实**，不能因为展示口径就去改它。
 * 有官方成交记录时调用方给出白名单，这里只渲染白名单里的链接：缺口说明一字不动，
 * 只是不再把不列出的域名印出来。
 */
export default function ResearchIssues({ values, sources = [], visibleUrls }) {
  const groups = groupResearchIssues(values)
  const keep = url => !visibleUrls || visibleUrls.has(url)
  return <ul className="research-issues">{groups.map(group => <li key={group.message}><p>{group.message}</p>{group.urls.some(keep) && <details><summary>查看相关来源（{group.urls.filter(keep).length} 个）</summary><ul>{group.urls.filter(keep).map(url => <li key={url}><SourceLink url={url}>{sources.find(s => s.url === url)?.title || undefined}</SourceLink></li>)}</ul></details>}</li>)}</ul>
}

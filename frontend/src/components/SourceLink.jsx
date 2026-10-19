import React from 'react'

import { siteNameOf, sourceSummary } from '../services/siteNames'

export function safeWebUrl(value) {
  try {
    const url = new URL(String(value || ''))
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null
  } catch { return null }
}

/**
 * 一条来源链接。
 *
 * 用户口径：**不要把链接本身印出来**，而是简述它指向哪里——页面上任何默认可见的
 * 文字里都不出现 ``http(s)://``。所以：
 *   - ``href`` 仍是完整地址，照样能点开、能复制；
 *   - 悬停提示（``title``）原来直接放完整 URL，现在放同一句简述；
 *   - 调用方没给文字时，默认显示「站点名 · 页面类型」的简述（``page`` 由调用方给，
 *     例如中标公告 / 产品页），不再是域名。
 */
export default function SourceLink({ url, page = '', children }) {
  const href = safeWebUrl(url)
  if (!href) return <span>{children || '来源网址未提供'}</span>
  const summary = sourceSummary(href, page)
  return <a className="source-link" href={href} target="_blank" rel="noopener noreferrer" title={summary}>{children || summary}</a>
}

/**
 * Plain source/error text remains text. Only explicit HTTP(S) URLs become links.
 *
 * 链接文字原来是 ``https://…`` 域名（``ccgp.gov.cn`` 这种英文域名），现在换成中文
 * 站点名——用户在页面上看到的仍然是同一条来源，只是不再是一个看不懂的域名。
 */
export function LinkedText({ children }) {
  return String(children ?? '').split(/(https?:\/\/[^\s<>"'，；。（）]+)/g).map((part, index) => /^https?:\/\//.test(part)
    ? <SourceLink key={index} url={part}>查看来源（{siteNameOf(part)}）</SourceLink> : part)
}

import React from 'react'
import { ArrowRight, BookOpen } from 'lucide-react'
import { Link } from 'react-router-dom'

export default function StartPage() {
  return <main className="start-page">
    <div className="start-orbit" aria-hidden="true"><i /><i /><span /></div>
    <div className="start-content">
      <h1 aria-label="LabPilot"><span>Lab</span><strong>Pilot</strong></h1>
      <p className="start-tagline">实验室仪器采购选型工作台 —— 从采购场景到可比报告，每一步都保留来源。</p>
      <div className="start-actions">
        <Link className="start-secondary" to="/app/help"><BookOpen size={18} aria-hidden="true" /><span>使用说明</span></Link>
        <Link className="start-primary" to="/app/new"><span>开始使用</span><ArrowRight size={19} aria-hidden="true" /></Link>
      </div>
    </div>
  </main>
}

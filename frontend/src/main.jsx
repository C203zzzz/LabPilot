import React from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import AppShell from './app/AppShell'
import { TaskProvider } from './features/TaskContext'
import NewSelection from './pages/NewSelection'
import StartPage from './pages/StartPage'
import TaskWorkspace from './pages/TaskWorkspace'
import { ApiSettingsPage, HelpPage, HistoryPage, ReviewPage } from './pages/WorkspacePages'
import './styles/index.css'
import './styles/workbench.css'

function App() {
  return <TaskProvider><BrowserRouter><Routes>
    <Route path="/" element={<StartPage />} />
    <Route path="/app" element={<AppShell />}>
      <Route index element={<Navigate to="new" replace />} />
      <Route path="new" element={<NewSelection />} />
      <Route path="task/:taskId" element={<TaskWorkspace />} />
      <Route path="history" element={<HistoryPage />} />
      <Route path="review" element={<ReviewPage />} />
      <Route path="api" element={<ApiSettingsPage />} />
      <Route path="help" element={<HelpPage />} />
    </Route>
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes></BrowserRouter></TaskProvider>
}

createRoot(document.getElementById('root')).render(<React.StrictMode><App /></React.StrictMode>)

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import ApplyPage from './ApplyPage.tsx'
import ChatPage from './ChatPage.tsx'
import './index.css'

const queryClient = new QueryClient()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/apply" element={<ApplyPage />} />
          <Route path="/chat/:token" element={<ChatPage />} />
          {/* The reviewer link is /apply?code=…; anything else lands on the apply page too. */}
          <Route path="*" element={<Navigate to="/apply" replace />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)

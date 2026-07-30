import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/montserrat'
import './index.css'
import App from './App'
import { DataProvider, NewVideosProvider, SystemProvider } from './data'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <DataProvider>
      <SystemProvider>
        <NewVideosProvider>
          <App />
        </NewVideosProvider>
      </SystemProvider>
    </DataProvider>
  </StrictMode>,
)

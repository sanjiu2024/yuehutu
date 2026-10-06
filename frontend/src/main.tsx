/** 入口：挂载 React 应用。 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { initTheme } from './components'
// 顺序有讲究：令牌与基础层、组件层、页面通用件 —— 后面的可以覆盖前面的。
import './styles.css'
import './components.css'
import './pages/pages.css'

// 在挂载前把主题贴到 <html> 上：否则首帧会先渲染深色，浅色用户会看到一下闪白/闪黑
initTheme()

const container = document.getElementById('root')
if (!container) {
  throw new Error('找不到 #root 容器')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

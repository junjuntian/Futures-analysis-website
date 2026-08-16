@echo off
rem 本地样式 + 生产数据的开发模式(视觉改版验收用)。
rem 代理直连生产 IP 的原因与坑见 vite.config.ts 顶部注释。
cd /d "%~dp0.."
npm run dev:proddata

@echo off
rem Local styles + production data proxy. See vite.config.ts header comments.
rem ASCII only in this file: cmd parses UTF-8 Chinese as GBK garbage and breaks.
cd /d "%~dp0.."
npm run dev:proddata

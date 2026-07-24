# futures VPS Phase 1 部署记录

## 状态

尚未部署。2026-07-24 20:59 +08:00 已通过 MCP SSH 对 `futures` 别名执行只读核对，确认 VPS 当前缺少 Docker 与 Docker Compose，因此 Phase 1 容器部署验证阻塞。

## VPS 基础状态

| 项目 | 结果 |
| --- | --- |
| SSH 别名 | `futures` |
| 主机名 | `localhost` |
| 内核/系统 | Linux `7.0.0-22-generic`，Ubuntu，x86_64 |
| 根分区 | 25G，总用量约 2.7G，约 21G 可用 |
| 内存 | 约 956MiB，总可用约 762MiB |
| Swap | 约 495MiB |
| 监听端口 | SSH 22、DNS stub 53、chrony 323 等基础端口 |

## 容器与服务状态

| 项目 | 结果 |
| --- | --- |
| Docker | 未安装：`docker: command not found` |
| Docker Compose | 未安装：`docker-compose: command not found` |
| 当前容器 | 无法查询；Docker 不可用 |
| 本项目容器 | 未创建 |
| 本项目镜像 | 未构建 |
| 本项目端口 | 未占用 |

## 数据库迁移状态

未执行。原因：本阶段 PostgreSQL 计划通过 Docker Compose 启动；当前 VPS 未安装 Docker/Compose。

## 健康检查状态

| 检查项 | 状态 | 原因 |
| --- | --- | --- |
| API live | 未执行 | 服务未部署 |
| API ready | 未执行 | 服务未部署，数据库未启动 |
| API version | 未执行 | 服务未部署 |
| 前端页面 | 未执行 | 服务未部署 |
| Nginx 代理 | 未执行 | 服务未部署 |
| Worker | 未执行 | 服务未部署 |

## 风险与阻塞

- `futures` VPS 当前内存约 956MiB，运行 PostgreSQL、Rust API、Worker、Nginx 和后续浏览器/OCR 组件时资源可能偏紧；Phase 1 可先做基础容器验证，后续 Playwright/noVNC/OCR 需要重新评估容量。
- VPS 未安装 Docker/Compose。安装 Docker 属于改变远端系统状态，需要用户明确确认后再执行。
- 当前未读取环境变量、Cookie、Token、数据库连接串或任何密钥。

## 下一步

等待用户确认是否允许在 `futures` VPS 安装 Docker/Compose，或指定其他部署方式。确认前不得伪造部署完成状态。

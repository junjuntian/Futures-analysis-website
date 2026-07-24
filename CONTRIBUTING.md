# 贡献指南

## 基本规则

- 使用 Conventional Commits。
- 功能改动使用 feature branch，不直接在 `main` 上开发。
- Phase 1 只允许工程基础能力，不实现正式业务功能。
- 提交前运行本地验证，并记录失败原因。

## 本地验证

```powershell
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build
docker compose config
```

Rust 命令在 `rust/` 目录执行；pnpm 命令在项目根目录执行。

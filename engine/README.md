# 机构资金信号引擎

跟随金银双强八席位持仓变化的每日信号系统。规则来自 `research/REPORT_AU_v1.md`
的全部回测结论,运营者 2026-08-11 逐条拍板定稿。

**生产地址**:`http://<VPS>:8088/smart-money/`

## 文件

| 文件 | 作用 |
| --- | --- |
| `smart_money.py` | 引擎。数据加载、事件检测、权重、成本、信号、告警,输出 `signals.json` |
| `index.html` | 页面。纯静态,读同目录 `signals.json` 渲染,四个页签 |
| `run-smart-money.sh` | 生产运行脚本:导出数据 → 容器内算 → 原子替换 `signals.json` |
| `data/london_*.csv` | 伦敦金银现货历史(运营者提供,金银比长期基线) |
| `data/gold_silver_ratio.csv` | 金银比缓存,每次运行自动增量更新(运行时产生) |

## 设计要点

**无状态全量重放**。每次运行从 2015 年重算全部交易,得出当前持仓与信号。
幂等(已验证:两次运行除时间戳外字节一致)、可重跑、漏跑一次下次自动补回,
不存在状态文件损坏或漂移的可能。

**T+1 时序**。席位数据 15:00 后可得 → 当晚计算 → 次日开盘执行,买卖同。
回测与生产用同一套时序,不存在未来函数。

**参数集中在 `RULES`**,不散落代码。改规则只改这一处。

## 生产部署

```
/opt/futures-platform/smart-money/
  smart_money.py  index.html  run-smart-money.sh
  data/    金银比缓存
  web/     nginx 服务目录(index.html + signals.json)
  tmp/     CSV 中转(每次运行后清理)
```

- 运行入口:`/usr/local/sbin/run-smart-money`
- 定时:`/etc/cron.d/futures-smart-money`,工作日 18:10 与 22:10(两次采集之后)
- 运行环境:复用 collector 镜像(自带 pandas 3.0),**不需要在宿主机装 Python 包**
- nginx:`deploy/nginx/nginx.conf` 的 `location /smart-money/`,
  compose 挂载 `./smart-money/web:/usr/share/nginx/smart-money:ro`
- 日志:`/var/log/futures-smart-money.log`

### 回滚

```bash
# nginx 与 compose 改动回滚(备份于同目录)
cd /opt/futures-platform
cp docker-compose.yml.bak-smartmoney docker-compose.yml
cp deploy/nginx/nginx.conf.bak-smartmoney deploy/nginx/nginx.conf
docker compose up -d nginx
# 停止定时
rm /etc/cron.d/futures-smart-money
```

引擎与页面是独立目录 + 一个 nginx location,不改动 API/前端/数据库,
回滚不影响平台任何既有功能。

## 数据依赖与已知问题

引擎读 `price_history` / `seat_history` 的 AU、AG 两个品种,依赖每日采集 cron 入库。

**采集侧待修(2026-08-11 发现)**:`akshare_v1` 自 2026-07-31 起向 `seat_history`
写入与交易所官方**重复**的席位行,且 `change` 字段全为空(2155 行)。
引擎已在 `clean_seat` 中按「官方优先 + change 非空优先」去重自保,
但更根本的修复应在采集侧:官方数据可得时不应重复写入无 change 的行。

该问题若不处理,任何直接读 `seat_history` 而未做优先级去重的下游,
都会把 ΔNet 算成 0 —— 发现时它正让白银持仓产生一个错误的卖出信号。

## 本地开发

```bash
cd engine
ENGINE_SOURCE=csv CSV_DIR=../research/data ENGINE_OUT=web/signals.json \
  ENGINE_DATA=data python smart_money.py
python -m http.server 8899 --directory web   # 浏览器打开 127.0.0.1:8899
```

本地 CSV 由 `research/data/` 下的 au/ag 导出文件提供(见 research 目录说明)。

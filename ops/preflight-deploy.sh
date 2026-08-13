#!/usr/bin/env bash
# 部署前置检查。全绿才打印出可直接执行的 dispatch 命令。
#
# 为什么是脚本而不是一份清单：2026-08-10 那天连续三次部署失败，漏的都是清单上写着
# 的东西——迁移没写 schema_versions、新文件没装进发布包、建完镜像又推了提交。
# 靠人记得看清单挡不住这类事，靠脚本才挡得住。每一条检查下面都注明它是被哪次失败
# 教出来的，删任何一条之前先想清楚那次失败会不会回来。
#
#   用法：ops/preflight-deploy.sh              # 只检查并打印命令
#         ops/preflight-deploy.sh --dispatch   # 检查通过后直接触发部署
#
# 有一类前置条件脚本查不了，写在 docs/DEPLOY_PREFLIGHT.md 里，部署前必须一起过。

set -Eeuo pipefail

REPO=junjuntian/Futures-analysis-website
BRANCH=phase/05-spread-analytics
DISPATCH=0
[ "${1:-}" = "--dispatch" ] && DISPATCH=1

failures=0
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; failures=$((failures + 1)); }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

cd "$(dirname "$0")/.."

step "一、本地仓库状态"

# 只拦会进构建产物的那些路径。
#
# 运营者在另一个会话里做席位因子预测模型，产出落在 research/，那个目录会长期是
# 改动状态。把它也算进来的话这道检查每次都红——而一个总是红的门禁很快就会被无视，
# 那就等于没做。所以按「改了会不会影响部署出去的东西」来分。
# 根级 package.json/pnpm-lock.yaml/pnpm-workspace.yaml 也进前端镜像
#（frontend/Dockerfile COPY 了它们），漏了会放过「只改根 lockfile」的构建。
DEPLOYED_PATHS='^(rust|frontend|collector|deploy|backfill|engine|\.github)/|^docker-compose|^(package\.json|pnpm-lock\.yaml|pnpm-workspace\.yaml)$'
dirty_deployed=$(git status --porcelain --untracked-files=no |
  awk '{print $2}' | grep -E "$DEPLOYED_PATHS" || true)
if [ -z "$dirty_deployed" ]; then
  pass "会进构建产物的路径没有未提交改动"
else
  fail "这些改动会进构建产物却没提交：$(printf '%s' "$dirty_deployed" | head -3 | tr '\n' ' ')"
fi

# || true：工作树完全干净时 grep 空匹配退出 1，会被 pipefail 打死——只在
# 「一个未提交文件都没有」的日子才现形，第一次撞上时脚本连第二节都没跑到。
other=$(git status --porcelain | awk '{print $2}' | grep -Ev "$DEPLOYED_PATHS" | head -3 | tr '\n' ' ' || true)
if [ -n "$other" ]; then
  printf '  \033[33m·\033[0m 其他改动（不进构建，仅提醒）：%s\n' "$other"
fi

SHA=$(git rev-parse HEAD)
SHORT=${SHA:0:7}
pass "HEAD = $SHORT"

# 教训（2026-08-10 第一次失败）：部署工作流要求 acceptance_sha 等于它检出的提交，
# 也就是分支头。建完镜像后又推了一个文档提交，HEAD 前移，部署被守卫直接拒绝。
if git fetch -q origin "$BRANCH" 2>/dev/null &&
   [ "$(git rev-parse "origin/$BRANCH")" = "$SHA" ]; then
  pass "已推送，origin/$BRANCH 与 HEAD 一致"
else
  fail "HEAD 未推送或与 origin/$BRANCH 不一致——部署会被 acceptance_sha 守卫拒绝"
fi

# 前端真编一遍。
#
# 教训（2026-08-12）：给建仓过程页加掉榜标记时漏了一个 </span>，vue-tsc 和 vitest
# 都过了——它们不做模板结构检查——直到 CI 的 vite build 才报「Element is missing
# end tag」。为一个漏掉的闭合标签白等了一轮 CI。
# 这一步六秒，CI 那轮十几分钟；preflight 存在的意义就是把这种往返省掉。
# Rust 格式。CI 第一步就跑 cargo fmt --check,本地一秒钟的事,却害过一整轮 CI
# (2026-08-13:改密码端点提交,唯一的问题是两处换行)。
if (cd rust && cargo fmt --check >/dev/null 2>&1); then
  pass "cargo fmt 格式一致"
else
  fail "cargo fmt --check 不过——在 rust/ 下跑一次 cargo fmt 即可"
fi

# clippy，**命令必须与 CI 逐字一致**（含 -- -D warnings）。
# 教训（2026-08-13）：本地跑的是不带 -D warnings 的 clippy，一条
# single_element_loop 在本地只是提示、在 CI 是编译失败——「我本地跑过了」
# 与「CI 会过」是两件事，除非两边跑的是同一条命令。
if (cd rust && cargo clippy --workspace --all-targets -- -D warnings >/dev/null 2>&1); then
  pass "cargo clippy 无警告"
else
  fail "cargo clippy 有警告——CI 用 -D warnings 会判失败，本地跑 (cd rust && cargo clippy --workspace --all-targets -- -D warnings) 看详情"
fi

if [ -d frontend/node_modules ]; then
  if (cd frontend && npx vite build >/dev/null 2>&1); then
    pass "前端 vite build 通过"
  else
    fail "前端 vite build 失败——CI 会在同一步挂掉，先在本地跑 (cd frontend && npx vite build) 看报错"
  fi
else
  printf '  \033[33m·\033[0m 跳过前端构建：frontend/node_modules 不在\n'
fi

# 自建 runner 的 workflow 绝不能被 fork 触发。
#
# 仓库 2026-08-13 转为公开,而自建 runner 就是生产服务器本身(runner 用户在
# docker 组里,等同 root)。任何 pull_request / pull_request_target 触发,都意味着
# 陌生人 fork 之后提个 PR 就能在生产机上跑他写的代码——读数据库、读
# /etc/futures-platform/secrets、读部署 SSH 私钥。当天封堵时核查过:窗口期内
# 没有 PR、没有外部触发的运行,没被利用。
#
# 这条与网站上不上 TLS 无关:TLS 管的是浏览器到服务器那段,这条路是从 GitHub
# 直接进服务器的。守住它的唯一办法就是让触发条件里永远没有那两个词。
# 2026-08-13 起全部作业跑在 GitHub 托管 runner 上。这道检查守的是「别搬回去」:
# 自建 runner 就是生产服务器本身,一旦它再出现,fork PR 就又能碰到生产数据。
# 真要搬回自建,先把 pull_request 触发的风险想清楚,再把这条一起改掉。
self_hosted=$(grep -ln "runs-on:.*self-hosted" .github/workflows/*.yml || true)
if [ -z "$self_hosted" ]; then
  pass "全部作业跑在托管 runner 上(公开仓库不限分钟数)"
else
  fail "这些 workflow 又回到自建 runner:$(printf '%s' "$self_hosted" | tr '
' ' ')——那台机器是生产服务器"
fi

# 有自建 runner 时,fork 触发等于把生产交出去;现在没有了,但这条留着,
# 因为搬回去的那天多半没人想起来重新加。
for wf in .github/workflows/*.yml; do
  grep -q "runs-on:.*self-hosted" "$wf" || continue
  triggers=$(sed -n '/^on:/,/^[a-z]/p' "$wf")
  if printf '%s' "$triggers" | grep -qE "^[[:space:]]*pull_request(_target)?:"; then
    fail "$(basename "$wf") 在自建 runner 上接受 fork 触发——公开仓库下等于把生产服务器交出去"
  fi
done

# compose 的卷引用两个方向都要对得上。CI 有 `docker compose config`，但本机通常
# 没有 docker，而这条用 YAML 解析就能查——不必等 CI 跑十几分钟才知道。
# 教训：删 worker 时把 object_storage_data 卷一起删了，api 那边还挂着，
# CI 报 `service "api" refers to undefined volume` 整个红。
compose_problems=$(python - <<'PY' 2>&1 || true
import io, yaml
class L(yaml.SafeLoader):
    pass
L.add_multi_constructor("!", lambda loader, suffix, node: None)
for path in ["docker-compose.yml", "docker-compose.production.yml"]:
    doc = yaml.load(io.open(path, encoding="utf-8"), Loader=L) or {}
    declared = set((doc.get("volumes") or {}).keys())
    used = set()
    for name, svc in (doc.get("services") or {}).items():
        for mount in (svc or {}).get("volumes") or []:
            if not isinstance(mount, str):
                continue
            source = mount.split(":", 1)[0]
            if source[:1] in "/.$" or not source:
                continue
            used.add(source)
            if source not in declared:
                print(f"{path}: 服务 {name} 挂了未声明的卷 {source}")
    for name in declared - used:
        print(f"{path}: 卷 {name} 声明了但没有服务在用")
PY
)
if [ -z "$compose_problems" ]; then
  pass "compose 的卷引用两个方向都对得上"
else
  fail "compose 卷引用有问题：$compose_problems"
fi

step "二、迁移"

# 教训（第二次失败）：部署在打包那步报 migration_missing_version_record。
# 这条 CI 已经查了，这里再查一遍是因为它便宜，而且 CI 可能被跳过。
oldest=$(grep -oE 'rust/migrations/[0-9]+_[a-z0-9_]+\.sql' .github/workflows/deploy-futures.yml |
  sed 's|rust/migrations/||' | sort | head -1)
before_migrations=$failures
for file in rust/migrations/*.sql; do
  name=$(basename "$file")
  [ "$name" \< "$oldest" ] && continue
  version=${name%%_*}
  grep -q "insert into schema_versions" "$file" ||
    { fail "$name 没有写 schema_versions"; }
  grep -q "'$version'" "$file" ||
    { fail "$name 的 schema_versions 版本号与文件名不符"; }
  grep -q "^begin;" "$file" ||
    { fail "$name 没有 begin;——断言失败时会留下半应用的 schema"; }
  # 教训：忘了把新迁移加进这份显式清单，就是代码上线而 schema 没到。
  # 工作流注释里记着这正是 leg-order 那次每个查询回 500 的成因。
  grep -q "rust/migrations/$name" .github/workflows/deploy-futures.yml ||
    { fail "$name 没有列进 deploy-futures.yml 的迁移清单"; }
done
# 只看这一节新增了多少失败，否则前面一节红了这里就永远不打印结论。
[ "$failures" -eq "$before_migrations" ] &&
  pass "全部迁移都自报版本号、带事务、已列入发布清单"

step "三、发布包内容"

# 教训：project-history.sql 加了却没装进发布包，run-collector.sh 找不到它，
# 只会打一行 PROJECTION_SKIPPED，然后每天安静地不投影。
# 那个脚本本身已随 DEC-049 删除，但这条门禁留着——它守的是「deploy/collector/ 下
# 每个文件都得装进发布包」，与具体是哪个文件无关。
for file in deploy/collector/*; do
  # 只看文件。跑过 pytest 之后这里会多出一个 __pycache__ 目录，它不是要发布的东西，
  # 报成「没装进发布包」是误报——门禁误报多了就会被无视，跟没有一样。
  [ -f "$file" ] || continue
  name=$(basename "$file")
  case "$name" in *.pyc | .*) continue ;; esac
  if grep -q "deploy/collector/$name" .github/workflows/deploy-futures.yml; then
    pass "deploy/collector/$name 已装进发布包"
  else
    fail "deploy/collector/$name 没装进发布包——线上会找不到它，且多半不报错"
  fi
done

# 教训：2026-08-12 查出服务器上的 backfill/parsers.py 是仓库版的旧副本，还在按
# 「品种上市年」补三位郑商所代码的世纪，把 2026 年的 FG608 解析成 FG1608，与真实
# 存在过的 2016 年合约撞进同一条序列。仓库那份早修好了，改动从来没走到机器上。
# 现在这些脚本随发布包下发，清单是显式的——所以必须有人盯着「新增了却没列进去」。
# .sql 也要:load_sanhe_seats.sql 曾是「仓库有、发布包没有」——文档把它当装载
# 入口,机器上却根本找不到这个文件,重灌历史只能手抄。
for file in backfill/*.py backfill/*.sql; do
  [ -f "$file" ] || continue
  name=$(basename "$file")
  if grep -q "backfill/$name" .github/workflows/deploy-futures.yml; then
    pass "backfill/$name 已装进发布包"
  else
    fail "backfill/$name 没装进发布包——机器上会继续跑旧副本，而且悄无声息"
  fi
done

# cron 断言已改成 e2e 里对发布包源文件整文件 diff（2026-08-12），验收与产物
# 不可能再漂移，原来的「提取 grep 模式逐个比对」检查随之退役。这里只守住
# 一件事：e2e 还在做那个 diff，没人把它删掉换回逐条断言。
if grep -q 'diff -u "$RELEASE_DIR/deploy/collector/futures-collector.cron"' rust/tests/phase_4a_e2e.sh; then
  pass "验收对 cron 做整文件比对"
else
  fail "phase_4a_e2e.sh 不再整文件比对 cron——逐条断言会重蹈时刻漂移与漏行"
fi

# 验收写库用的来源标签必须与生产逐字一致。上一版验收写着退役的 akshare 来源，
# 打开 live 采集必失败并整库回滚——H06 的教训。
#
# 比的对象 2026-08-13 随直灌改了：现在决定库里 source 列的是装载脚本的
# -v source_code，不再是 collector 里的常量（那个常量只用来拼 CSV 文件名）。
# 两边都用 `|| true` 兜住：守卫查不到东西时必须说话，不能让 set -e 把整个
# preflight 静默掐掉——那样操作者只看到流程突然消失，还以为跑完了。
prod_seats=$(grep -o 'source_code=[a-z0-9_]*' deploy/collector/run-collector.sh | cut -d= -f2 | sort -u || true)
e2e_seats=$(grep -o 'source_code=[a-z0-9_]*' rust/tests/phase_4a_e2e.sh | cut -d= -f2 | sort -u || true)
if [ -n "$prod_seats" ] && [ "$prod_seats" = "$e2e_seats" ]; then
  pass "验收与生产写的是同一个来源标签（$prod_seats）"
else
  fail "来源标签漂移：生产=($prod_seats) 验收=($e2e_seats)——验收会断言在一个没人写过的来源上"
fi

# 验收装载 CSV 用的文件名约定也必须与生产一致：采集器按 <来源>-<数据集>-<日期>.csv
# 命名，生产与验收各自手写这个模式，写歪了就是「采集成功、装载找不到文件」，
# 而两边都不报错。
prod_csv=$(grep -oF 'DCE-seat_positions_v1-$COLLECTION_DATE.csv' deploy/collector/run-collector.sh | head -1 || true)
e2e_csv=$(grep -oF 'DCE-seat_positions_v1-$COLLECTION_DATE.csv' rust/tests/phase_4a_e2e.sh | head -1 || true)
if [ -n "$prod_csv" ] && [ -n "$e2e_csv" ]; then
  pass "验收与生产的席位 CSV 文件名约定一致"
else
  fail "席位 CSV 文件名约定对不上：生产=($prod_csv) 验收=($e2e_csv)"
fi

step "四、CI 与镜像"

ci_status=$(gh api "repos/$REPO/actions/runs?head_sha=$SHA&per_page=50" \
  --jq '[.workflow_runs[] | select(.name == "CI")] | first | "\(.status) \(.conclusion)"' 2>/dev/null || echo "none")
if [ "$ci_status" = "completed success" ]; then
  pass "CI 在 $SHORT 上通过"
else
  fail "CI 在 $SHORT 上不是 completed success（当前：$ci_status）"
fi

build_id=$(gh api "repos/$REPO/actions/runs?head_sha=$SHA&per_page=50" \
  --jq '[.workflow_runs[] | select(.name == "Container images" and .conclusion == "success")] | first | .id' 2>/dev/null || echo "null")
if [ "$build_id" = "null" ] || [ -z "$build_id" ]; then
  fail "$SHORT 上没有成功的镜像构建——先跑 container-images 工作流"
  digests=""
else
  pass "镜像构建 $build_id 成功"
  # 日志拿不到就明确红——原来 2>/dev/null 加 set -e 会静默退出，操作者只看到
  # 流程突然消失，还以为脚本跑完了。
  if ! build_log=$(gh run view "$build_id" --log 2>&1); then
    fail "读取构建日志失败（gh run view $build_id）——没有日志就提不出 digest，不要盲部署"
    build_log=""
  fi
  digests=$(printf '%s\n' "$build_log" |
    grep -o "pushing manifest for ghcr.io[^ ]*@sha256:[0-9a-f]*" | sort -u)
  # 构建工作流会打出 image-built-from <镜像> <提交>：复用旧镜像时那不是本次 HEAD。
  # deploy 要据此核验「复用的镜像与本次 sha 在其输入路径上逐字节一致」，所以
  # 把非 HEAD 的来源收进 image_sources 传给它。
  image_sources=""
  while read -r _ built_image built_sha; do
    [ -z "$built_image" ] && continue
    if [ "$built_sha" != "$SHA" ]; then
      image_sources="${image_sources:+$image_sources,}$built_image=$built_sha"
      printf '  \033[33m·\033[0m %s 镜像复用自 %s（输入路径未变，deploy 会核验等价）\n' \
        "$built_image" "${built_sha:0:7}"
    fi
  done <<EOF_SOURCES
$(printf '%s\n' "$build_log" | grep -oE "image-built-from [a-z]+ [0-9a-f]{40}" | sort -u)
EOF_SOURCES
  for image in api frontend collector; do
    if printf '%s\n' "$digests" | grep -q -- "-$image:sha-$SHA@"; then
      pass "$image 镜像已发布"
    else
      # 教训：重启 runner 会杀掉正在跑的作业，四个镜像会缺一个而整体仍显示 success。
      fail "$image 镜像缺失——构建可能被中断过，重跑失败作业"
    fi
  done
fi

digest_of() {
  printf '%s\n' "$digests" | grep -- "-$1:sha-$SHA@" | sed 's/.*@//' | head -1
}

# 原来这里检查自建 runner 在线且空闲——那时它只有一台,被别的作业占着就得等,
# 重启它还会杀掉正在跑的作业。2026-08-13 全部搬到托管 runner 之后没有这个约束:
# 每个作业各拿一台全新机器,不排队也不互相干扰。这一节整体退役。

if [ "$failures" -gt 0 ]; then
  printf '\n\033[31m%d 项未通过，先修好再部署。\033[0m\n' "$failures"
  printf '脚本查不了的前置条件见 docs/DEPLOY_PREFLIGHT.md，一并过一遍。\n'
  exit 1
fi

# run_live_collection 按改动路径决定，不再无脑 false。
#
# false 是 2026-08-09 加的提速开关（跳过三次真采省约一小时），但 preflight 把它
# 写死成了默认——采集器本身的改动也被跳过，坏 collector 直接上线，要等第二天
# cron 才真实失败。改成：与上一次成功部署的提交做 diff，碰了采集链路
#（collector/、deploy/collector/、backfill/、采集相关 e2e）就必须真采一轮。
# 拿不到上次部署的提交时宁可 true：多花一小时，好过跳过唯一的集成门。
last_deployed_sha=$(gh api "repos/$REPO/actions/runs?per_page=20" \
  --jq '[.workflow_runs[] | select(.name == "Deploy futures" and .conclusion == "success")] | first | .head_sha' 2>/dev/null || echo "")
run_live=true
if [ -n "$last_deployed_sha" ] && [ "$last_deployed_sha" != "null" ] &&
   git cat-file -e "$last_deployed_sha" 2>/dev/null; then
  collector_changes=$(git diff --name-only "$last_deployed_sha" HEAD -- \
    collector/ deploy/collector/ backfill/ rust/tests/phase_4a_e2e.sh | head -5)
  if [ -z "$collector_changes" ]; then
    run_live=false
    pass "采集链路自上次部署（${last_deployed_sha:0:7}）无改动，run_live_collection=false"
  else
    pass "采集链路有改动，run_live_collection=true：$(printf '%s' "$collector_changes" | tr '\n' ' ')"
  fi
else
  pass "查不到上次成功部署的提交，保守 run_live_collection=true"
fi

# 最近一个已收盘的工作日，给验收用。
collection_date=$(python -c "
from datetime import date, timedelta
day = date.today() - timedelta(days=1)
while day.weekday() > 4:
    day -= timedelta(days=1)
print(day.isoformat())
")

printf '\n\033[32m全部通过。\033[0m\n'
printf '\n还有一件脚本挡不住的事：\033[1m从现在到部署完成，不要再推任何提交\033[0m。\n'
printf '推了 HEAD 就变了，部署会被 acceptance_sha 守卫拒绝，镜像得整个重建。\n\n'

command=$(cat <<EOF
gh workflow run deploy-futures.yml --ref $BRANCH \\
  -f git_sha=$SHA \\
  -f acceptance_sha=$SHA \\
  -f api_digest=$(digest_of api) \\
  -f frontend_digest=$(digest_of frontend) \\
  -f collector_digest=$(digest_of collector) \\
  -f collection_date=$collection_date \\
  -f provision_collector=false \\
  -f run_live_collection=$run_live${image_sources:+ \\
  -f image_sources=$image_sources}
EOF
)

if [ "$DISPATCH" -eq 1 ]; then
  printf '正在触发部署……\n'
  eval "$command"
else
  printf '%s\n' "$command"
  printf '\n（加 --dispatch 可直接触发）\n'
fi

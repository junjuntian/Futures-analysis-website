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
DEPLOYED_PATHS='^(rust|frontend|collector|deploy|\.github)/|^docker-compose'
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
  build_log=$(gh run view "$build_id" --log 2>/dev/null)
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
  for image in api worker frontend collector; do
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

step "五、自建 runner"

runner=$(gh api "repos/$REPO/actions/runners" \
  --jq '.runners[] | "\(.status) busy=\(.busy)"' 2>/dev/null | head -1 || echo "unknown")
case "$runner" in
  "online busy=false") pass "runner 在线且空闲" ;;
  "online busy=true")  fail "runner 正在跑别的作业——此刻重启它会把那个作业杀掉" ;;
  *)                   fail "runner 状态异常：$runner" ;;
esac

if [ "$failures" -gt 0 ]; then
  printf '\n\033[31m%d 项未通过，先修好再部署。\033[0m\n' "$failures"
  printf '脚本查不了的前置条件见 docs/DEPLOY_PREFLIGHT.md，一并过一遍。\n'
  exit 1
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
  -f worker_digest=$(digest_of worker) \\
  -f frontend_digest=$(digest_of frontend) \\
  -f collector_digest=$(digest_of collector) \\
  -f collection_date=$collection_date \\
  -f provision_collector=false \\
  -f run_live_collection=false${image_sources:+ \\
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

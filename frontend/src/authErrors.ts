/**
 * 把后端的错误码翻成人话。
 *
 * 后端对所有 400 都回同一句 `message: "request is invalid"`——真正说明问题的是
 * `code`。2026-08-13 新站首次建号时,密码差了几位,界面只显示「request failed: 400」,
 * 运营者无从判断是密码、用户名还是 token 有问题,只能来问。
 *
 * 密码规则不写死在这里的话,提示就永远只能说「不合格」而说不出「要几位」。
 * 这几个数字与 rust/apps/api/src/auth.rs 的 validate_password / normalize_username
 * 一致,改那边要一起改这边——测试里有一条守着这个对应关系。
 */

export const PASSWORD_MIN = 15
export const PASSWORD_MAX = 128
export const USERNAME_MIN = 3
export const USERNAME_MAX = 64

const MESSAGES: Record<string, string> = {
  password_policy: `密码要 ${PASSWORD_MIN}–${PASSWORD_MAX} 位。长口令比复杂符号更难破,一句话加几个数字就够。`,
  password_common: '这个密码太常见,换一个不成词的。',
  password_unchanged: '新密码和当前密码一样。',
  username_policy: `用户名要 ${USERNAME_MIN}–${USERNAME_MAX} 个字符。`,
  bootstrap_token_invalid: 'Bootstrap Token 不对。复制时注意别漏字符或带上空格。',
  bootstrap_unavailable: '这台服务器没有配 bootstrap token,初始化通道是关的。',
  bootstrap_closed: '初始化通道已经用过了,不能再建号。要加账号请从已有账号进去。',
  bootstrap_state_missing: '数据库里没有初始化状态记录,迁移可能没跑全。',
  invalid_credentials: '用户名或密码不对。',
  account_disabled: '这个账号已被停用。',
  rate_limited: '尝试太频繁,等一会儿再试。',
  csrf_invalid: '页面状态过期了,刷新后重试。',
  origin_not_allowed: '请求来源不被接受——如果你是从别的域名或 IP 打开的,请用正式域名访问。'
}

/** 拿不到 code 时至少说清楚是哪一类失败,不要只甩一个数字。 */
const BY_STATUS: Record<number, string> = {
  400: '提交的内容不合要求。',
  401: '需要先登录。',
  403: '没有权限。',
  409: '与当前状态冲突。',
  429: '请求太频繁,稍后再试。',
  500: '服务器出错了,稍后重试;如果一直这样请查看服务日志。'
}

/**
 * @param code 后端 `data.code`
 * @param status HTTP 状态码
 */
export function authErrorText(code: string | undefined, status: number): string {
  if (code && MESSAGES[code]) return MESSAGES[code]
  const base = BY_STATUS[status] ?? `请求失败(HTTP ${status})。`
  // 认不出的 code 也要带出来:让人能拿它来搜或者报给我,好过一句「失败了」。
  return code ? `${base}(${code})` : base
}

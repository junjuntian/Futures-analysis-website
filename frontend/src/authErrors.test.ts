import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  authErrorText,
  PASSWORD_MAX,
  PASSWORD_MIN,
  USERNAME_MAX,
  USERNAME_MIN
} from './authErrors'

// vitest 跑在 jsdom 里,import.meta.url 不是 file: scheme,取不到脚本自身路径。
// 从工作目录往上找:本地在 frontend/ 下跑,CI 有可能在仓库根跑。
const AUTH_RS = ['../rust/apps/api/src/auth.rs', 'rust/apps/api/src/auth.rs']
  .map((candidate) => resolve(process.cwd(), candidate))
  .find(existsSync)

describe('登录与初始化的错误提示', () => {
  it('把后端错误码翻成能照着改的话', () => {
    // 2026-08-13 新站首次建号:密码差几位,界面只显示「request failed: 400」,
    // 运营者无从判断是密码、用户名还是 token 的问题。提示必须说清楚差在哪。
    const text = authErrorText('password_policy', 400)
    expect(text).toContain(String(PASSWORD_MIN))
    expect(text).not.toContain('400')
  })

  it('认不出的错误码也要带出来,不能只说「失败了」', () => {
    const text = authErrorText('some_new_code', 400)
    expect(text).toContain('some_new_code')
  })

  it('没有错误码时按状态码给出一句人话', () => {
    expect(authErrorText(undefined, 401)).toContain('登录')
    expect(authErrorText(undefined, 599)).toContain('599')
  })

  it('密码与用户名的位数与后端 auth.rs 一致', () => {
    // 这两处数字分居 Rust 与 TypeScript。改了一边不改另一边,提示就会理直气壮地
    // 报一个错的位数——比不提示更坏,因为人会照着它改然后再次被拒。
    expect(AUTH_RS, '找不到 rust/apps/api/src/auth.rs——路径候选要跟着改').toBeDefined()
    const source = readFileSync(AUTH_RS!, 'utf8')

    const password = source.match(/!\((\d+)\.\.=(\d+)\)\.contains\(&length\)/)
    expect(password, 'auth.rs 里的密码长度校验没找到,正则要跟着改').not.toBeNull()
    expect(Number(password![1])).toBe(PASSWORD_MIN)
    expect(Number(password![2])).toBe(PASSWORD_MAX)

    const username = source.match(
      /normalized\.len\(\) < (\d+) \|\| normalized\.len\(\) > (\d+)/
    )
    expect(username, 'auth.rs 里的用户名长度校验没找到,正则要跟着改').not.toBeNull()
    expect(Number(username![1])).toBe(USERNAME_MIN)
    expect(Number(username![2])).toBe(USERNAME_MAX)
  })

  it('覆盖 auth.rs 里实际会抛给用户的错误码', () => {
    // 后端新增一个 BadRequest 码而这里没跟上,用户看到的就是「(some_new_code)」
    // 这种半成品提示。清单不必完全相等——有些码只在内部路径出现——但常见的
    // 这几个必须在。
    expect(AUTH_RS, '找不到 rust/apps/api/src/auth.rs——路径候选要跟着改').toBeDefined()
    const source = readFileSync(AUTH_RS!, 'utf8')
    for (const code of ['password_policy', 'password_common', 'username_policy']) {
      expect(source, `auth.rs 里应当有 ${code}`).toContain(`"${code}"`)
      expect(authErrorText(code, 400), `${code} 没有中文提示`).not.toContain(code)
    }
  })
})

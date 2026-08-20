/**
 * 拉取失败时的**分流提示**。
 *
 * 2026-08-20 事故:机构资金整页 403。文案一律是「请确认信号引擎已运行」——
 * 而那天引擎跑得好好的,文件也是当天新写的,坏的是权限(600,容器里非 root 的
 * nginx 读不到)。这句话把排查方向直接引偏,当时是靠旁边那个 `HTTP 403`
 * 才一眼定位的。
 *
 * 所以:**兜底文案不替人猜原因**。状态码已经说清了是哪一类问题就照它分流,
 * 分不出来就老实说分不出来,别给一个听着笃定的错方向。
 *
 * 抽成模块是为了能测——埋在 `<script setup>` 里的函数导不出来(见 shelf.ts)。
 */
export function failureHint(cause: unknown): string {
  const code = /HTTP (\d{3})/.exec(String(cause))?.[1]
  if (code === '403') return '文件存在但服务器读不到,多半是权限问题,不是引擎没跑。'
  if (code === '404') return '信号文件不存在,确认引擎是否产出过这个品种。'
  if (code && code.startsWith('5')) return '服务器出错,看 nginx 日志。'
  return '网络或文件格式问题,看浏览器控制台。'
}

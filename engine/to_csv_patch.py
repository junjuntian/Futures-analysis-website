# -*- coding: utf-8 -*-
"""给 VPS 上的 /opt/futures-platform/to_csv.py 打 --since 增量补丁(幂等)。"""
import pathlib

p = pathlib.Path("/opt/futures-platform/to_csv.py")
s = p.read_text(encoding="utf-8")
if "--since" in s:
    print("已打过补丁,跳过")
    raise SystemExit(0)

s = s.replace(
    '    ap.add_argument("--limit", type=int, default=0)\n    args = ap.parse_args()',
    '    ap.add_argument("--limit", type=int, default=0)\n'
    '    # 每日增量:只解析文件名日期 >= SINCE 的原始文件(文件名即 YYYYMMDD 戳)。\n'
    '    ap.add_argument("--since", default="")\n'
    '    args = ap.parse_args()\n'
    '    since_stamp = args.since.replace("-", "")',
    1)
s = s.replace(
    '            files = sorted(glob.glob(str(RAW / sub / pattern)))\n'
    '            if args.limit:',
    '            files = sorted(glob.glob(str(RAW / sub / pattern)))\n'
    '            if since_stamp:\n'
    '                files = [f for f in files\n'
    '                         if "".join(ch for ch in Path(f).stem if ch.isdigit()) >= since_stamp]\n'
    '            if args.limit:',
    1)
assert "--since" in s and "since_stamp" in s
p.write_text(s, encoding="utf-8")
print("补丁完成")

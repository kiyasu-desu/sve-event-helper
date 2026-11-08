"""从 bundle 提取的 CJK 串里还原"省-市"地区清单。

背景：APP 的地区选择数据没有独立接口，也不在 APK assets 里；
但它以 "广东省-广州市" 这种形式存在于 bundle 的字符串表中。
字符串表是拼接的，所以原始匹配会带上前一条串的尾巴（如
"九郎葛生能世辽宁省-葫芦岛市"），需要用规范的省级名称做锚点切出来。
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import OrderedDict

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SRC = os.path.join(PROJECT_ROOT, "recon", "cjk_all.txt")
OUT = os.path.join(PROJECT_ROOT, "app", "client", "data", "areas.json")

# 34 个省级行政区（含直辖市与特别行政区）。用作切分锚点：
# 形如 "<噪声>辽宁省-葫芦岛市" 只保留最后的省级名称及其后的市。
PROVINCES = [
    "北京市", "天津市", "上海市", "重庆市",
    "河北省", "山西省", "辽宁省", "吉林省", "黑龙江省",
    "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省",
    "河南省", "湖北省", "湖南省", "广东省", "海南省",
    "四川省", "贵州省", "云南省", "陕西省", "甘肃省", "青海省", "台湾省",
    "内蒙古自治区", "广西壮族自治区", "西藏自治区", "宁夏回族自治区", "新疆维吾尔自治区",
    "香港特别行政区", "澳门特别行政区",
]
# 允许自治区简写出现在串里
PROV_ALIASES = PROVINCES + ["内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门"]

# bundle 字符串表里缺失的省级行政区（4 个直辖市与自治区）的常见城市。
# 属于公开行政区划信息；写盘后会用 /urs/tournament/list 抽样验证是否被服务端接受。
SUPPLEMENT = {
    "北京市": ["北京市"],
    "天津市": ["天津市"],
    "上海市": ["上海市"],
    "重庆市": ["重庆市"],
    "内蒙古自治区": ["呼和浩特市", "包头市", "鄂尔多斯市", "赤峰市", "通辽市"],
    "广西壮族自治区": ["南宁市", "柳州市", "桂林市", "梧州市", "北海市"],
    "西藏自治区": ["拉萨市", "日喀则市", "昌都市", "林芝市"],
    "宁夏回族自治区": ["银川市", "石嘴山市", "吴忠市", "固原市", "中卫市"],
    "新疆维吾尔自治区": ["乌鲁木齐市", "克拉玛依市", "吐鲁番市", "哈密市"],
    "台湾省": ["台北市", "高雄市", "台中市", "台南市"],
    "香港特别行政区": ["香港特别行政区"],
    "澳门特别行政区": ["澳门特别行政区"],
}


def splice(raw: str) -> tuple[str, str] | None:
    """从粘连串里切出 (省, 市)。切不出返回 None。"""
    # 找到最后一个省级名称，并确保它后面紧跟 "-"
    best = None
    for p in PROV_ALIASES:
        idx = raw.rfind(p)
        if idx < 0:
            continue
        after = raw[idx + len(p):]
        if after.startswith("-") and len(after) > 1:
            if best is None or idx > best[0]:
                best = (idx, p, after[1:])
    if not best:
        return None
    _idx, prov, rest = best
    # 市部分应是"XX市"/"XX自治州"等，取到第一个"市"为止
    m = re.match(r"^([\u4e00-\u9fa5]{1,10}?(?:市|自治州|地区|盟|特别行政区))", rest)
    if not m:
        return None
    city = m.group(1)
    # 直辖市形态：上海市-上海市
    full_prov = prov
    for p in PROVINCES:
        if prov in p:
            full_prov = p
            break
    return full_prov, city


def main() -> int:
    txt = open(SRC, encoding="utf-8", errors="replace").read()
    raw_matches = re.findall(r"[\u4e00-\u9fa5]{2,12}?省-[\u4e00-\u9fa5]{2,12}?市", txt)

    areas: "OrderedDict[str, list[str]]" = OrderedDict()
    seen_raw = 0
    for raw in raw_matches:
        got = splice(raw)
        if not got:
            continue
        prov, city = got
        seen_raw += 1
        areas.setdefault(prov, [])
        if city not in areas[prov]:
            areas[prov].append(city)

    # 直辖市单独处理：bundle 里是 "上海市-上海市" 这种
    for m in re.finditer(r"(北京市|天津市|上海市|重庆市)-(北京市|天津市|上海市|重庆市)", txt):
        prov, city = m.group(1), m.group(2)
        areas.setdefault(prov, [])
        if city not in areas[prov]:
            areas[prov].append(city)

    # 补充：bundle 字符串表里没有出现的省级行政区（4 个直辖市与自治区）。
    # 这些是公开行政区划信息，不是猜测；写入后会用接口抽样验证。
    for prov, cities in SUPPLEMENT.items():
        areas.setdefault(prov, [])
        for c in cities:
            if c not in areas[prov]:
                areas[prov].append(c)

    data = {
        "source": "recon/cjk_all.txt（APP bundle 字符串表提取）",
        "note": "地区名格式为 '<省>-<市>'，与 /urs/tournament/list 的 area 参数一致",
        "provinces": [{"name": p, "cities": sorted(set(c))} for p, c in areas.items()],
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)

    print("  原始匹配 %d 条，成功切分 %d 条" % (len(raw_matches), seen_raw))
    print("  得到 %d 个省/直辖市、%d 个城市" % (
        len(areas), sum(len(v) for v in areas.values())))
    print()
    for p, cs in list(areas.items())[:40]:
        print("  %-14s %s" % (p, " ".join(cs[:8]) + (" …" if len(cs) > 8 else "")))
    print()
    print("  已写入", os.path.relpath(OUT, PROJECT_ROOT))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

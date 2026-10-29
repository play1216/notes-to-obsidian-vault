# -*- coding: utf-8 -*-
"""建库后全量校验。任何一项非零都会以退出码 1 结束，便于串进自动化。

用法:
    python verify_vault.py --config <config.json>

刻意避开的三个坑（别"优化"回去）:
  * 不能用 Path(x).stem 取文件名 —— 条目号 "1.1-3 排列组合" 里的点会被当扩展名，stem 返回 "1"
  * 双链目标里的分隔符有 `\\|` 和 `|` 两种写法，两种都要切
  * 模板里的 IMG_XXXX / X.Y-N 是占位符，不该计入坏链
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

PLACEHOLDER = re.compile(r"^(IMG_X+|X\.Y-N|\d{4}-XX-XX|知识点[一二三四五]|方法[一二三四五]|（.+）|\.\.\.)$")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    a = ap.parse_args()

    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    src, F = cfg["source"], cfg["fields"]
    L = cfg.get("layout", {})
    L.setdefault("items", "10 题目")
    L.setdefault("methods", "20 方法")
    L.setdefault("pages", "30 原页")
    L.setdefault("assets", "40 附件")
    L.setdefault("templates", "90 模板")

    VAULT = Path(cfg["vault"]["path"])
    SRC = Path(src["root"])
    if not VAULT.is_dir():
        sys.exit(f"[中止] 库不存在: {VAULT}")

    fails = []
    mds = list(VAULT.rglob("*.md"))
    names = set()
    for f in VAULT.rglob("*"):
        if f.is_file() and ".obsidian" not in str(f):
            names.add(f.name)
            names.add(f.stem)

    qnotes = [f for f in mds if L["items"] in str(f) and not f.name.startswith("_")]
    print(f"[1] 条目笔记文件数: {len(qnotes)}")

    ids, bad_fm = [], []
    for f in qnotes:
        t = f.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", t, re.S)
        mid = re.search(r'^' + re.escape(F["id"]) + r': "?([^"\n]+)"?$', m.group(1), re.M) if m else None
        if not mid:
            bad_fm.append(f.name)
            continue
        ids.append(mid.group(1))
    print(f"[2] 有 {F['id']} 的笔记: {len(ids)}｜无 frontmatter: {len(bad_fm)} {bad_fm[:3]}")
    if bad_fm:
        fails.append(f"{len(bad_fm)} 篇缺 frontmatter 或 {F['id']}")

    dup = [k for k, v in Counter(ids).items() if v > 1]
    print(f"[3] 重复{F['id']}: {dup if dup else '无'}")
    if dup:
        fails.append(f"重复{F['id']} {len(dup)} 个")

    src_ids = set()
    for f in SRC.glob(src.get("page_glob", "*.md")):
        src_ids |= set(re.findall(src["item_pattern"], f.read_text(encoding="utf-8"), re.M))
    src_ids = {x if isinstance(x, str) else x[0] for x in src_ids}
    miss, extra = sorted(src_ids - set(ids)), sorted(set(ids) - src_ids)
    print(f"[4] 源 {len(src_ids)}｜库 {len(set(ids))}｜缺 {len(miss)} {miss[:6]}｜多 {len(extra)} {extra[:6]}")
    if miss or extra:
        fails.append(f"条目集合不一致：缺 {len(miss)} 多 {len(extra)}")

    # 双链
    # 注意：代码块和行内代码里的 [[...]] 在 Obsidian 里不解析，必须跳过，否则示例文字会被误报
    def strip_code(t):
        t = re.sub(r"```[\s\S]*?```", "", t)
        return re.sub(r"`[^`\n]*`", "", t)

    # 模板目录放的是占位内容，不参与坏链检查
    link_files = [f for f in mds if L["templates"] not in str(f)]
    broken, link_total = Counter(), 0
    for f in link_files:
        for m in re.finditer(r"!?\[\[([^\]]+)\]\]", strip_code(f.read_text(encoding="utf-8"))):
            target = re.split(r"\\?\||#", m.group(1))[0].strip()
            if not target:
                continue
            link_total += 1
            name = target[:-3] if target.endswith(".md") else target
            if name not in names and not PLACEHOLDER.match(name):
                broken[(name, f.name)] += 1
    print(f"[5] 双链 {link_total} 条｜未解析 {sum(broken.values())}")
    for (name, where), c in list(broken.items())[:12]:
        print(f"     ✗ [[{name}]] ← {where}")
    if broken:
        fails.append(f"{sum(broken.values())} 条坏链")

    # 照片
    photos = [m.group(1) for f in qnotes
              for m in re.finditer(r'^' + re.escape(F["photo"]) + r': "?([^"\n]+)"?$', f.read_text(encoding="utf-8"), re.M)]
    missp = sorted({p for p in photos if not (VAULT / L["assets"] / "原图" / p).exists()})
    print(f"[6] {F['photo']}字段 {len(photos)} 处｜文件缺失 {len(missp)} {missp[:5]}")
    if missp:
        fails.append(f"{len(missp)} 张照片找不到")

    # 公式完整性
    srcq = {}
    for f in SRC.glob(src.get("page_glob", "*.md")):
        t = f.read_text(encoding="utf-8")
        hdrs = list(re.finditer(src["item_pattern"], t, re.M))
        for i, m in enumerate(hdrs):
            seg = t[m.end(): hdrs[i + 1].start() if i + 1 < len(hdrs) else len(t)]
            cut = re.search(src.get("item_cut_pattern", r"^##\s"), seg, re.M)
            if cut:
                seg = seg[:cut.start()]
            qid = m.group(1) if m.lastindex else m.group(0)
            srcq[qid] = seg.count("$")
    bad_dollar = []
    for f in qnotes:
        t = f.read_text(encoding="utf-8")
        m = re.search(r'^' + re.escape(F["id"]) + r': "?([^"\n]+)"?$', t, re.M)
        if not m:
            continue
        qid = m.group(1)
        body = t.split("\n---\n", 1)[1] if "\n---\n" in t else t
        if body.count("$") != srcq.get(qid, -1):
            bad_dollar.append((qid, srcq.get(qid), body.count("$")))
    print(f"[7] 公式 $ 数量不一致: {len(bad_dollar)} {bad_dollar[:6]}")
    if bad_dollar:
        fails.append(f"{len(bad_dollar)} 条公式数量不符（正文可能被截断）")

    # 残留元数据
    left = [f.name for f in qnotes
            if re.search(r"^-\s*\*\*(" + "|".join(map(re.escape, src.get("meta_fields", []))) + r")\*\*",
                         f.read_text(encoding="utf-8"), re.M)]
    print(f"[8] 正文残留元数据行: {len(left)} {left[:3]}")
    if left:
        fails.append(f"{len(left)} 篇正文里还留着元数据行")

    print(f"[9] 原页 {len(list((VAULT / L['pages']).glob('*.md')))} 篇｜"
          f"方法 {len(list((VAULT / L['methods']).glob('*.md')))} 篇｜"
          f"附件 {len(list((VAULT / L['assets']).rglob('*.*')))} 个")
    low = sum(1 for f in qnotes if "章节待确认: true" in f.read_text(encoding="utf-8"))
    print(f"[10] 章节待确认 {low} 条")
    print(f"[11] 章节分布: {dict(Counter(f.parent.name for f in qnotes))}")

    print()
    if fails:
        print("=== 未通过")
        for x in fails:
            print(f"  - {x}")
        sys.exit(1)
    print("=== 全部通过")


if __name__ == "__main__":
    main()

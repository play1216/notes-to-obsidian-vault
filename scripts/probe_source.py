# -*- coding: utf-8 -*-
"""探查源目录结构，输出诊断报告 + 待补的配置项。

用法:
    python probe_source.py --config <config.json> [--max-samples 8]

在正式建库前跑。它只读，不写任何东西。
重点报告四类"会让内容悄悄丢失"的结构问题：
  1. 不匹配 item_pattern 的 ### 标题  -> 该题连同正文会被整块漏掉
  2. 不在 section_names/section_alias 里的独立加粗小标题 -> 那段正文会被当前言丢弃
  3. 页级 ## 小节粘在最后一题后面 -> 会把页级内容串进题目
  4. 标签只在反引号里（`#xxx`）-> Obsidian 不识别为标签，迁移时必须改写
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


def load(cfg_path):
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    src = cfg["source"]
    root = Path(src["root"])
    if not root.is_dir():
        sys.exit(f"[中止] source.root 不存在: {root}")
    return cfg, root


def bar(n, total, width=28):
    filled = 0 if not total else round(n / total * width)
    return "█" * filled + "░" * (width - filled)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--max-samples", type=int, default=8)
    a = ap.parse_args()

    cfg, root = load(a.config)
    src = cfg["source"]
    fields = cfg["fields"]
    page_glob = src.get("page_glob", "*.md")
    date_re = re.compile(src.get("page_date_pattern", r"^#\s*(\d{4}-\d{2}-\d{2})\s*$"), re.M)
    item_re = re.compile(src.get("item_pattern", r"^###\s*(.+)$"), re.M)
    cut_re = re.compile(src.get("item_cut_pattern", r"^##\s"), re.M)
    tag_re = re.compile(src.get("tag_pattern", r"`(#[^`]+)`"))
    meta_re = re.compile(
        r"^\s*-\s*\*\*(" + "|".join(map(re.escape, src.get("meta_fields", []))) + r")\*\*\s*[：:]\s*(.+)$", re.M
    )
    page_hdr_re = re.compile(
        r"^\s*-\s*\*\*(" + "|".join(map(re.escape, src.get("page_header_fields", ["照片"]))) + r")\*\*\s*[：:]\s*(.+)$",
        re.M,
    )
    section_names = set(src.get("section_names", []))
    section_alias = src.get("section_alias", {})
    known_sections = section_names | set(section_alias)

    page_files = sorted(root.glob(page_glob))
    if not page_files:
        sys.exit(f"[中止] {root}/{page_glob} 没有匹配到任何文件")

    print(f"=== 源目录 {root}")
    print(f"页文件 {len(page_files)} 个（glob: {page_glob}）\n")

    items = []           # (page, item_id, note, body)
    bad_titles = []      # 真·漏抓：落在所有条目块之外的 ###
    sub_titles = []      # 块内子标题：无害，只是正文里的分级
    bad_sections = []    # (page, 小标题, 所在条目)
    page_level_cut = 0
    dates = []
    page_hdr_hits = Counter()
    photo_refs = []

    for f in page_files:
        text = f.read_text(encoding="utf-8")
        dm = date_re.search(text)
        dates.append(dm.group(1) if dm else f.stem)

        seen_photo = False
        for m in page_hdr_re.finditer(text):
            page_hdr_hits[m.group(1)] += 1
            if m.group(1) == "照片" and not seen_photo:
                photo_refs.append((f.name, re.sub(r"[（(].*$", "", m.group(2)).strip()))
                seen_photo = True

        matched = list(item_re.finditer(text))
        matched_starts = {m.start() for m in matched}
        spans = [(m.end(), matched[i + 1].start() if i + 1 < len(matched) else len(text))
                 for i, m in enumerate(matched)]

        # 不匹配 item_pattern 的 ### —— 只有落在所有条目块之外的才是真漏抓
        for h in re.finditer(r"^###\s*(.+)$", text, re.M):
            if h.start() in matched_starts:
                continue
            (sub_titles if any(s <= h.start() < e for s, e in spans) else bad_titles).append(
                (f.name, h.group(0).strip()))

        for i, m in enumerate(matched):
            start, end = spans[i]
            seg = text[start:end]
            cut = cut_re.search(seg)
            if cut:
                page_level_cut += 1
                seg = seg[: cut.start()]
            note = (m.group(2).strip().strip("（）()") if m.lastindex and m.lastindex >= 2 else "")
            items.append((f.name, m.group(1), note, seg))

            # 独立成行的加粗小标题：过滤掉正文强调（**第一步：xxx** 这类）
            for sm in re.finditer(r"^\*\*([^*\n]{1,24})\*\*\s*$", seg, re.M):
                name = sm.group(1).strip()
                if name in known_sections:
                    continue
                if "：" in name or ":" in name or re.match(r"^第[一二三四五六七八九十0-9]+步", name):
                    continue
                if name[-1] in "。！？.!?" or len(name) > 14:
                    continue
                bad_sections.append((f.name, name, m.group(1)))

    print(f"日期范围 {min(dates)} → {max(dates)}｜{len(set(dates))} 个不同日期")
    print(f"切分锚点命中 {len(items)} 条（item_pattern: {src.get('item_pattern')}）\n")

    # --- 1. 漏抓的 ### 标题 ---
    print(f"--- [1] 落在条目块之外的 ### 标题（真漏抓）: {len(bad_titles)}")
    if bad_titles:
        print("    !! 这些内容会被整块漏掉，必须调整 item_pattern 或加别名")
        for p, t in bad_titles[: a.max_samples]:
            print(f"    ✗ {p}: {t}")
    else:
        print("    ✓ 没有")
    if sub_titles:
        print(f"    另有序目块内的子标题 {len(sub_titles)} 个（如 "
              f"{sub_titles[0][1]!r}），属于正文分级，不影响完整性")
    print()

    # --- 2. 未识别的小标题 ---
    sec_cnt = Counter(s for _, s, _ in bad_sections)
    print(f"--- [2] 未识别的独立加粗小标题: {len(sec_cnt)} 种 / {len(bad_sections)} 处")
    if sec_cnt:
        print("    正文不会丢（会作为前言段落原样保留），但这些不会变成 ## 小节")
        print("    想保留层级就加进 section_alias")
        for s, c in sec_cnt.most_common(a.max_samples):
            where = next(i for p, ss, i in bad_sections if ss == s)
            print(f"    · **{s}** ×{c}   首个出现在 {where}")
    else:
        print("    ✓ 全部已识别")
    print()

    # --- 3. 页级 ## 截断 ---
    print(f"--- [3] 页级 ## 小节截断: {page_level_cut} 处")
    if page_level_cut:
        print("    已按 item_cut_pattern 截断，页级内容不会串进题目（需确认这是期望行为）")
    print()

    # --- 4. 元数据与标签 ---
    field_cnt = Counter()
    kf, mf = fields.get("knowledge", "知识点"), fields.get("method", "方法")
    tag_by_field = {kf: Counter(), mf: Counter()}
    stars = Counter()
    for _, _, _, body in items:
        for m in meta_re.finditer(body):
            k, v = m.group(1), m.group(2)
            field_cnt[k] += 1
            if k in tag_by_field:
                for t in tag_re.findall(v):
                    tag_by_field[k][t.lstrip("#")] += 1
            elif k == fields.get("difficulty", "难度"):
                sm = re.search(src.get("difficulty_pattern", r"[★☆]+"), v)
                stars[sm.group(0).count("★") if sm else 0] += 1

    print(f"--- [4] 元数据字段覆盖（共 {len(items)} 题）")
    for k in src.get("meta_fields", []):
        n = field_cnt.get(k, 0)
        print(f"    {k:6} {bar(n, len(items))} {n}")
    print()

    for name, tally in tag_by_field.items():
        uniq = len(tally)
        once = sum(1 for v in tally.values() if v == 1)
        print(f"--- [5] {name}标签: {uniq} 个唯一 / 总计 {sum(tally.values())} 次")
        if uniq:
            print(f"    只出现 1 次: {once} ({once * 100 // uniq}%)  "
                  f"≥2 次: {uniq - once}  ≥3 次: {sum(1 for v in tally.values() if v >= 3)}")
            if once * 100 // max(uniq, 1) > 70:
                print("    !! 一次性标签占绝大多数：这批标签是描述而非索引，不要原样当标签用，需归并")
            print("    高频: " + "、".join(f"{k}({v})" for k, v in tally.most_common(8)))
        print()

    print(f"--- [6] 难度分布: " + "、".join(f"{k}星×{v}" for k, v in sorted(stars.items(), reverse=True)))
    print()

    # --- 7. 章节规则覆盖率（算法与 build_vault.py 完全一致，否则预测无效）---
    chapters = cfg.get("chapters", [])
    if chapters:
        fallback = cfg.get("fallback_chapter", chapters[-1]["name"])
        margin = cfg.get("chapter_confidence_margin", 2)
        ch_names = [c["name"] for c in chapters]
        kf = fields.get("knowledge", "知识点")

        def chapter_of(tags):
            score = Counter()
            for i, t in enumerate(tags):
                w = 5 if i == 0 else (3 if i == 1 else 1)
                for ch in chapters:
                    if any(k in t for k in ch["keywords"]):
                        score[ch["name"]] += w
                        break
            if not score:
                return fallback, 0
            ranked = sorted(score.items(), key=lambda kv: (kv[1], -ch_names.index(kv[0])), reverse=True)
            return ranked[0][0], ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0)

        hit, miss, low = Counter(), [], []
        for _, qid, _, body in items:
            tags = []
            for m in meta_re.finditer(body):
                if m.group(1) == kf:
                    tags = [t.lstrip("#") for t in tag_re.findall(m.group(2))]
                    break
            ch, conf = chapter_of(tags)
            hit[ch] += 1
            if not tags:
                miss.append((qid, tags[:3]))
            elif conf <= margin:
                low.append((qid, ch, tags[:2]))
        print(f"--- [7] 章节分布（顺序敏感 + 位置加权，与建库算法一致）")
        for ch in chapters:
            n = hit.get(ch["name"], 0)
            print(f"    {ch['name']:8} {bar(n, len(items))} {n}  {n * 100 // max(len(items), 1)}%")
        print(f"    无知识点字段（将落入「{fallback}」）: {len(miss)}")
        for qid, tags in miss[: a.max_samples]:
            print(f"    ? {qid}  知识点: {'、'.join(tags) or '（空）'}")
        print(f"    领先分差 ≤{margin}（会打「章节待确认」）: {len(low)}")
        for qid, ch, tags in low[: a.max_samples]:
            print(f"    ~ {qid} → {ch}  {'、'.join(tags)}")
        print()

    # --- 8. 附件核对 ---
    photo_dir = cfg.get("attachments", {}).get("photo_dir", "")
    if photo_refs and photo_dir:
        pd = Path(photo_dir)
        have = {f.name for f in pd.glob("*")} if pd.is_dir() else set()
        alias = cfg.get("attachments", {}).get("photo_alias", {})
        refs = {alias.get(p, p) for _, p in photo_refs}
        missing = sorted(refs - have)
        unused = sorted(have - refs)
        print(f"--- [8] 照片: 引用 {len(refs)} 个｜目录实有 {len(have)} 个")
        print(f"    引用但缺失: {len(missing)} {missing[: a.max_samples]}")
        print(f"    目录多余（可能是重拍/补拍）: {len(unused)} {unused[: a.max_samples]}")
        if unused:
            print("    !! 多余文件先人工确认是不是某页的重拍，再决定是否加 photo_alias")
    else:
        print("--- [8] 照片: 未配置 attachments.photo_dir，跳过核对")
    print()

    # --- 结论 ---
    print("=== 结论")
    blockers = []
    if bad_titles:
        blockers.append(f"{len(bad_titles)} 个 ### 标题落在条目块外（会丢内容）")
    if photo_refs and photo_dir:
        pd = Path(photo_dir)
        have = {f.name for f in pd.glob("*")} if pd.is_dir() else set()
        alias = cfg.get("attachments", {}).get("photo_alias", {})
        miss_n = len({alias.get(p, p) for _, p in photo_refs} - have)
        if miss_n:
            blockers.append(f"{miss_n} 张照片引用不到")
    if blockers:
        print("先修这些问题再建库，否则内容会静默丢失：")
        for b in blockers:
            print(f"  - {b}")
    else:
        print("结构对齐，可以进入建库。")
    if sec_cnt:
        print(f"（提示：{len(sec_cnt)} 种小标题未映射为 ## 小节，内容不受影响）")


if __name__ == "__main__":
    main()

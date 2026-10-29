# -*- coding: utf-8 -*-
"""按页组织的 md 笔记 -> Obsidian 原子笔记库（一题一篇 / 一条一篇）。

用法:
    python build_vault.py --config <config.json> [--dry-run]

安全约束（不要改）:
  * 源目录只读，全程不写、不删、不移动源文件
  * 目标库已存在且非空时中止，需显式 --force
  * --force 重建前会自动暂存 00 总览 / 90 模板 下手写的页，重建后写回
"""
import argparse
import json
import re
import shutil
import sys
import tempfile
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

DEFAULT_LAYOUT = {
    "overview": "00 总览",
    "items": "10 题目",
    "methods": "20 方法",
    "pages": "30 原页",
    "assets": "40 附件",
    "templates": "90 模板",
}
GENERATED = {"首页", "复习台", "统计"}   # 00 总览 下由脚本生成、重建时可覆盖的页


# ---------- 文本工具 ----------
def yq(s):
    """YAML 安全输出：遇到会被误解析的字符就加引号"""
    s = str(s).strip()
    if s == "" or re.search(r"^[\s\-\?\:\,\[\]\{\}\#\&\*\!\|\>\'\"\%\@\`]", s) \
       or ": " in s or s.endswith(":") or s.lower() in ("true", "false", "null", "~", "yes", "no"):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def clean_name(s):
    s = re.sub(r'[\\/:*?"<>|#\[\]]', "·", s).strip()
    return s[:60] or "未命名"


def strip_md(s):
    s = s or ""
    s = s.replace("`", "").replace("**", "")
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    return s.strip()


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg.setdefault("layout", {})
    for k, v in DEFAULT_LAYOUT.items():
        cfg["layout"].setdefault(k, v)
    cfg.setdefault("fallback_chapter", "未分类")
    cfg.setdefault("chapter_confidence_margin", 2)
    cfg.setdefault("method_min_count", 2)
    cfg.setdefault("attachments", {})
    cfg["attachments"].setdefault("photo_alias", {})
    cfg["attachments"].setdefault("inline_width", 420)
    cfg["attachments"].setdefault("page_width", 520)
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--force", action="store_true", help="目标非空时清空重建（会暂存手写页）")
    ap.add_argument("--dry-run", action="store_true", help="只解析并报告，不写文件")
    a = ap.parse_args()

    cfg = load_config(a.config)
    src, F, A = cfg["source"], cfg["fields"], cfg["attachments"]
    L = cfg["layout"]

    SRC = Path(src["root"])
    VAULT = Path(cfg["vault"]["path"])
    if not SRC.is_dir():
        sys.exit(f"[中止] source.root 不存在: {SRC}")
    if not cfg["vault"]["path"]:
        sys.exit("[中止] vault.path 未配置")
    PHOTO = Path(A["photo_dir"]) if A.get("photo_dir") else None
    if PHOTO and not PHOTO.is_dir():
        print(f"[警告] photo_dir 不存在，跳过照片复制: {PHOTO}")
        PHOTO = None

    page_re = re.compile(src.get("page_date_pattern", r"^#\s*(\d{4}-\d{2}-\d{2})\s*$"), re.M)
    item_re = re.compile(src["item_pattern"], re.M)
    cut_re = re.compile(src.get("item_cut_pattern", r"^##\s"), re.M)
    tag_re = re.compile(src.get("tag_pattern", r"`(#[^`]+)`"))
    diff_re = re.compile(src.get("difficulty_pattern", r"[★☆]+"))
    short_re = re.compile(src.get("id_short_pattern", r"^(\d+\.\d+)"))
    meta_fields = src.get("meta_fields", [])
    meta_re = re.compile(r"^\s*-\s*\*\*(" + "|".join(map(re.escape, meta_fields)) + r")\*\*\s*[：:]\s*(.+)$", re.M)
    page_hdr_re = re.compile(
        r"^\s*-\s*\*\*(" + "|".join(map(re.escape, src.get("page_header_fields", ["照片"]))) + r")\*\*\s*[：:]\s*(.+)$", re.M
    )
    section_alias = dict(src.get("section_alias", {}))
    for s in src.get("section_names", []):
        section_alias.setdefault(s, s)
    sec_names = sorted(section_alias, key=len, reverse=True)
    sec_re = re.compile(r"^\*\*(" + "|".join(re.escape(n) for n in sec_names) + r")\*\*\s*$", re.M)

    CHAPTERS = [c["name"] for c in cfg.get("chapters", [])] or [cfg["fallback_chapter"]]
    CH_RULES = [(c["name"], c["keywords"]) for c in cfg.get("chapters", [])]
    MOTHERS = OrderedDict(cfg.get("methods", {}))

    def tags_of(line):
        return [t.lstrip("#") for t in tag_re.findall(line or "") if len(t) > 1]

    def stars(s):
        m = diff_re.search(s or "")
        return m.group(0).count("★") if m else 0

    def chapter_of(tags):
        """顺序敏感：每个标签命中第一条规则即定章，按位置加权，返回 (章节, 领先分差)"""
        score = Counter()
        for i, t in enumerate(tags):
            w = 5 if i == 0 else (3 if i == 1 else 1)
            for ch, kws in CH_RULES:
                if any(k in t for k in kws):
                    score[ch] += w
                    break
        if not score:
            return cfg["fallback_chapter"], 0
        ranked = sorted(score.items(), key=lambda kv: (kv[1], -CHAPTERS.index(kv[0])), reverse=True)
        return ranked[0][0], ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0)

    def mothers_of(tags):
        out = []
        for t in tags:
            for m, kws in MOTHERS.items():
                if any(k in t for k in kws):
                    if m not in out:
                        out.append(m)
                    break
        return out

    # ---------- 解析 ----------
    pages = []
    for f in sorted(SRC.glob(src.get("page_glob", "*.md"))):
        text = f.read_text(encoding="utf-8")
        dm = page_re.search(text)
        date = dm.group(1) if dm else f.stem
        hdr = {m.group(1): m.group(2).strip() for m in page_hdr_re.finditer(text)}
        photo = re.sub(r"[（(].*$", "", hdr.get("照片", "")).strip()
        photo = A["photo_alias"].get(photo, photo)

        hdrs = list(item_re.finditer(text))
        qs = []
        for i, m in enumerate(hdrs):
            seg = text[m.end(): hdrs[i + 1].start() if i + 1 < len(hdrs) else len(text)]
            cut = cut_re.search(seg)          # 页级小节不属于任何一条
            if cut:
                seg = seg[:cut.start()]
            note = m.group(m.lastindex).strip().strip("（）()") if m.lastindex and m.lastindex >= 2 else ""
            fields = {mm.group(1): mm.group(2).strip() for mm in meta_re.finditer(seg)}
            sec = sec_re.split(seg)
            chunks = []
            pre = "\n".join(l for l in sec[0].splitlines()
                            if not re.match(r"^\s*-\s*\*\*(" + "|".join(map(re.escape, meta_fields)) + r")\*\*", l)).strip()
            if pre:
                chunks.append(pre)          # 非标准小标题下的正文靠这里兜住，别丢
            for j in range(1, len(sec), 2):
                chunks.append("## " + section_alias[sec[j]] + "\n\n" + sec[j + 1].strip())
            qs.append({
                "id": m.group(1), "note": note, "fields": fields,
                "kp": tags_of(fields.get(F["knowledge"], "")),
                "mth": tags_of(fields.get(F["method"], "")),
                "body": "\n\n".join(chunks).strip(),
                "stars": stars(fields.get(F["difficulty"], "")),
            })
        pages.append({"date": date, "photo": photo, "qs": qs, "raw": text})

    all_q = [q for p in pages for q in p["qs"]]
    print(f"解析到 {len(all_q)} 条 / {len(pages)} 页")
    if not all_q:
        sys.exit("[中止] 一条都没解析出来，检查 item_pattern")

    # ---------- 命名 ----------
    for p in pages:
        for idx, q in enumerate(p["qs"]):
            q.update(date=p["date"], photo=p["photo"], idx=idx + 1, total=len(p["qs"]))
            q["chapter"], q["conf"] = chapter_of(q["kp"])
            q["mothers"] = mothers_of(q["mth"])
            m = short_re.match(q["id"])
            q["short"] = m.group(1) if m else ""
            q["fname"] = clean_name(f'{q["id"]} {q["kp"][0] if q["kp"] else "未分类"}')

    dup = Counter(q["fname"] for q in all_q)
    for q in all_q:
        if dup[q["fname"]] > 1:
            q["fname"] = clean_name(f'{q["id"]} {q["kp"][0] if q["kp"] else ""} {q["stars"]}星')
    id2name = {q["id"]: q["fname"] for q in all_q}

    mcount = Counter(m for q in all_q for m in q["mothers"])
    active = {m for m, c in mcount.items() if c >= cfg["method_min_count"]}

    if a.dry_run:
        print("\n[dry-run] 章节分布:", dict(Counter(q["chapter"] for q in all_q)))
        low = [(q["id"], q["chapter"], q["kp"][:2]) for q in all_q if q["conf"] <= cfg["chapter_confidence_margin"]]
        print(f"[dry-run] 章节待确认 {len(low)} 条:", low[:10])
        print("[dry-run] 母方法:", dict(sorted(mcount.items(), key=lambda kv: -kv[1])))
        print("[dry-run] 未写任何文件")
        return

    # ---------- 建库 ----------
    stash = None
    if VAULT.exists() and any(VAULT.iterdir()):
        if not a.force:
            sys.exit(f"[中止] 目标已存在且非空: {VAULT}\n       换路径，或加 --force 清空重建")
        stash = Path(tempfile.mkdtemp(prefix="vault_keep_"))
        for sub in (L["overview"], L["templates"]):
            d = VAULT / sub
            if not d.is_dir():
                continue
            for f in d.glob("*.md"):
                if f.stem in GENERATED and sub == L["overview"]:
                    continue
                shutil.copy2(f, stash / f"{sub.replace(' ', '_')}__{f.name}")
        print(f"已暂存手写页到 {stash}")
        shutil.rmtree(VAULT)

    for d in [L["overview"], L["items"], L["methods"], L["pages"],
              f'{L["assets"]}/原图', f'{L["assets"]}/条目图', L["templates"]]:
        (VAULT / d).mkdir(parents=True, exist_ok=True)
    for ch in CHAPTERS:
        (VAULT / L["items"] / ch).mkdir(parents=True, exist_ok=True)

    (VAULT / ".obsidian").mkdir(exist_ok=True)
    (VAULT / ".obsidian" / "app.json").write_text(json.dumps({
        "attachmentFolderPath": L["assets"], "newFileLocation": "folder",
        "newFileFolderPath": L["items"], "alwaysUpdateLinks": True,
        "useMarkdownLinks": False, "strictLineBreaks": False, "livePreview": True,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 附件 ----------
    n_photo = 0
    if PHOTO:
        for f in sorted(PHOTO.glob("*")):
            if f.is_file():
                shutil.copy2(f, VAULT / L["assets"] / "原图" / f.name)
                n_photo += 1
    # 单条配图目录：相对页文件所在目录，而不是源根目录
    img_sub = A.get("page_image_subdir", "图")
    page_dir = SRC / Path(src.get("page_glob", "*.md")).parent
    img_dir = page_dir / img_sub
    n_item_img = 0
    if img_dir.is_dir():
        for f in sorted(img_dir.glob("*")):
            if f.is_file():
                shutil.copy2(f, VAULT / L["assets"] / "条目图" / f.name)
                n_item_img += 1
    print(f"附件: 原图 {n_photo} 张 / 条目图 {n_item_img} 张")

    # ---------- 条目笔记 ----------
    def linkify(text, self_id):
        """把正文里的条目号、日期引用转成双链，同时保护公式与行内代码"""
        hold = []

        def keep(m):
            hold.append(m.group(0))
            return f"\x00{len(hold) - 1}\x00"

        text = re.sub(r"\$\$[\s\S]+?\$\$", keep, text)
        text = re.sub(r"\$[^$\n]+?\$", keep, text)
        text = re.sub(r"`[^`\n]*`", keep, text)
        text = re.sub(r"!\[\]\(图/([^)]+)\)", lambda m: f"![[{m.group(1)}|{A['inline_width']}]]", text)

        def q(m):
            qid = m.group(1)
            return f"[[{id2name[qid]}|{qid}]]" if qid in id2name and qid != self_id else qid

        text = re.sub(r"(?<![\d.])(\d{1,2}\.\d{1,2}-\d{1,2})(?![\d-])", q, text)
        text = re.sub(r"`?(\d{4}-\d{2}-\d{2})\.md`?", lambda m: f"[[{m.group(1)}]]", text)
        for i, s in enumerate(hold):
            text = text.replace(f"\x00{i}\x00", s)
        return text

    written = 0
    for q in all_q:
        ch_dir = VAULT / L["items"] / q["chapter"]
        L_ = ["---", f'{F["id"]}: "{q["id"]}"', f'{F["date"]}: {q["date"]}',
              f'{F["short"]}: "{q["short"]}"', f'{F["chapter"]}: {q["chapter"]}']
        if q["conf"] <= cfg["chapter_confidence_margin"]:
            L_.append("章节待确认: true")
        L_ += [f'{F["index_in_page"]}: {q["idx"]}', f'{F["count_in_page"]}: {q["total"]}',
               f'{F["type"]}: {yq(strip_md(q["fields"].get(F["type"], "")))}',
               f'{F["difficulty"]}: {q["stars"]}',
               f'{F["status"]}: {yq(strip_md(q["fields"].get(F["status"], "")))}',
               f'{F["source"]}: {yq(strip_md(q["fields"].get(F["source"], "")))}']
        if q["note"]:
            L_.append(f'{F["note"]}: {yq(q["note"])}')
        if q["photo"]:
            L_.append(f'{F["photo"]}: {yq(q["photo"])}')
        for key, vals in ((F["knowledge"], q["kp"]), (F["method"], q["mth"])):
            if vals:
                L_.append(f"{key}:")
                L_ += [f"  - {yq(v)}" for v in vals]
        L_ += ["tags:", "  - 条目", f"  - 章节/{q['chapter']}"]
        L_ += [f"  - 知识点/{clean_name(v)}" for v in q["kp"]]
        L_ += [f"  - 方法/{clean_name(v)}" for v in q["mth"]]
        L_ += ["---", "", f'# {q["id"]} {q["kp"][0] if q["kp"] else ""}'.rstrip(), ""]
        if q["photo"]:
            L_ += [f'![[{q["photo"]}|{A["inline_width"]}]]', ""]
        L_.append(f'> 原页 [[{q["date"]}]]｜本页第 {q["idx"]} 条（共 {q["total"]} 条）')
        ml = " · ".join(f"[[{m}]]" for m in q["mothers"] if m in active)
        if ml:
            L_.append(f"> 方法 {ml}")
        L_ += ["", linkify(q["body"], q["id"]), ""]
        (ch_dir / f'{q["fname"]}.md').write_text("\n".join(L_), encoding="utf-8")
        written += 1
    print(f"写入条目笔记 {written} 篇")

    # ---------- 原页 ----------
    for p in pages:
        out = ["---", "类型: 原页", f'{F["date"]}: {p["date"]}', "tags:", "  - 原页", "---", ""]
        if p["photo"]:
            out += [f'![[{p["photo"]}|{A["page_width"]}]]', ""]
        if p["qs"]:
            out.append("> 本页条目：" + " · ".join(f'[[{id2name[q["id"]]}|{q["id"]}]]' for q in p["qs"]))
        else:
            out.append("> 本页无条目（空页/续页）")
        out += ["", p["raw"]]
        (VAULT / L["pages"] / f'{p["date"]}.md').write_text("\n".join(out), encoding="utf-8")
    print(f"写入原页 {len(pages)} 篇")

    # ---------- 方法笔记 ----------
    mbyq = defaultdict(list)
    for q in all_q:
        for m in q["mothers"]:
            if m in active:
                mbyq[m].append(q)
    for m, qs in sorted(mbyq.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        kws = MOTHERS[m]
        aliases = sorted({t for q in qs for t in q["mth"] if any(k in t for k in kws)})
        out = ["---", "类型: 方法", f"条目数: {len(qs)}"]
        if aliases:
            out.append("别名:")
            out += [f"  - {yq(x)}" for x in aliases[:12]]
        out += ["tags:", "  - 方法", "---", "", f"# {m}", "", "## 用在哪些条目", "",
                "| 编号 | 章节 | 难度 | 知识点 |", "|---|---|---|---|"]
        for q in sorted(qs, key=lambda x: x["id"]):
            out.append(f'| [[{q["fname"]}\\|{q["id"]}]] | {q["chapter"]} | {"★" * q["stars"]} | '
                       + "、".join(q["kp"][:3]) + " |")
        out += ["", "## 原始标签", "", "、".join(f"`{x}`" for x in aliases) if aliases else "—", "",
                "> 本页由迁移脚本生成。**反链面板**会自动列出所有引用本方法的条目，比上面这张表更新更及时。", ""]
        (VAULT / L["methods"] / f"{clean_name(m)}.md").write_text("\n".join(out), encoding="utf-8")
    print(f"写入方法笔记 {len(mbyq)} 篇（阈值 ≥{cfg['method_min_count']} 条）")

    # ---------- 章节索引 ----------
    for ch in CHAPTERS:
        qs = [q for q in all_q if q["chapter"] == ch]
        if not qs:
            continue
        out = ["---", "类型: 章节索引", f"章节: {ch}", f"条目数: {len(qs)}", "tags:", "  - 索引", "---", "",
               f"# {ch}", "", f"共 {len(qs)} 条。", "",
               "| 编号 | 知识点 | 题型 | 难度 | 状态 |", "|---|---|---|---|---|"]
        for q in sorted(qs, key=lambda x: (x["date"], x["idx"])):
            out.append(f'| [[{q["fname"]}\\|{q["id"]}]] | ' + "、".join(q["kp"][:3])
                       + f' | {strip_md(q["fields"].get(F["type"], ""))} | {"★" * q["stars"]} | '
                       + f'{strip_md(q["fields"].get(F["status"], ""))} |')
        out.append("")
        (VAULT / L["items"] / ch / f"_{ch}.md").write_text("\n".join(out), encoding="utf-8")

    # ---------- 总览 ----------
    title = cfg["vault"].get("title", "笔记库")
    ch_stat = [(ch, len([q for q in all_q if q["chapter"] == ch])) for ch in CHAPTERS]
    diff = Counter(q["stars"] for q in all_q)
    stat = Counter(strip_md(q["fields"].get(F["status"], "")) for q in all_q)
    kp_tally = Counter(t for q in all_q for t in q["kp"])
    mt_tally = Counter(t for q in all_q for t in q["mth"])

    home = ["---", "类型: 首页", "tags:", "  - 索引", "---", "", f"# {title}", "",
            f"**{len(all_q)} 条** · {len(pages)} 页 · 覆盖 {all_q[0]['date']} 到 {all_q[-1]['date']}", ""]
    if cfg["vault"].get("intro"):
        home += [f"> {cfg['vault']['intro']}", ""]
    home += ["## 章节", "", "| 章节 | 条目 |", "|---|---|"]
    home += [f"| [[_{ch}\\|{ch}]] | {n} |" for ch, n in ch_stat if n]
    home += ["", "## 入口", "", "- [[复习台]]", "- [[统计]]", "- [[怎么用]]",
             "- [[可选查询（需 Dataview）]]", "- [[字段说明]]", "",
             "## 结构", "", "| 文件夹 | 放什么 |", "|---|---|",
             f'| `{L["items"]}` | {len(all_q)} 篇，一条一篇，按章节分 |',
             f'| `{L["methods"]}` | 母方法，条目靠双链挂过来 |',
             f'| `{L["pages"]}` | {len(pages)} 个原页，翻纸对照用 |',
             f'| `{L["assets"]}` | 原图与条目图 |',
             f'| `{L["templates"]}` | 新条目模板 |', ""]
    (VAULT / L["overview"] / "首页.md").write_text("\n".join(home), encoding="utf-8")

    todo = [q for q in all_q if "待摸底" in strip_md(q["fields"].get(F["status"], ""))]
    rt = ["---", "类型: 复习台", "tags:", "  - 索引", "---", "", "# 复习台", "",
          f"待摸底 **{len(todo)}** 条（共 {len(all_q)} 条）。按难度倒序，同难度按日期倒序——先啃硬的。", "",
          "> 这份是**生成时的快照**。要自动排期，见 [[可选查询（需 Dataview）]]。", "",
          "| # | 编号 | 章节 | 难度 | 知识点 | 方法 |", "|---|---|---|---|---|---|"]
    for i, q in enumerate(sorted(todo, key=lambda x: (-x["stars"], x["date"])), 1):
        rt.append(f'| {i} | [[{q["fname"]}\\|{q["id"]}]] | {q["chapter"]} | {"★" * q["stars"]} | '
                  + "、".join(q["kp"][:2]) + " | " + "、".join(q["mth"][:2]) + " |")
    rt.append("")
    (VAULT / L["overview"] / "复习台.md").write_text("\n".join(rt), encoding="utf-8")

    st = ["---", "类型: 统计", "tags:", "  - 索引", "---", "", "# 统计", "",
          f"生成于迁移时，数据源是 {len(all_q)} 条笔记的头部字段。", "",
          "## 难度分布", "", "| 难度 | 条目 |", "|---|---|"]
    st += [f"| {'★' * s} | {diff[s]} |" for s in range(5, 0, -1) if diff.get(s)]
    st += ["", "## 状态分布", "", "| 状态 | 条目 |", "|---|---|"]
    st += [f"| {k or '（空）'} | {v} |" for k, v in stat.most_common()]
    st += ["", "## 章节分布", "", "| 章节 | 条目 | 占比 |", "|---|---|---|"]
    st += [f"| {ch} | {n} | {n * 100 // len(all_q)}% |" for ch, n in ch_stat if n]
    st += ["", "## 高频知识点（前 25）", "", "| 知识点 | 出现 |", "|---|---|"]
    st += [f"| {k} | {v} |" for k, v in kp_tally.most_common(25)]
    st += ["", "## 高频方法标签（前 25）", "", "| 方法标签 | 出现 |", "|---|---|"]
    st += [f"| {k} | {v} |" for k, v in mt_tally.most_common(25)]
    st += ["", f"> 方法标签共 {len(mt_tally)} 个，其中只出现 1 次的有 "
           f"{sum(1 for v in mt_tally.values() if v == 1)} 个——这些是一次性描述，不是可复用索引。"
           f"`{L['methods']}` 里只收了出现 ≥{cfg['method_min_count']} 条的母方法。", ""]
    (VAULT / L["overview"] / "统计.md").write_text("\n".join(st), encoding="utf-8")

    # ---------- 模板与说明页 ----------
    subs = {
        "{{TITLE}}": title,
        "{{VAULT_PATH}}": str(VAULT),
        "{{SRC_PATH}}": str(SRC),
        "{{ID}}": F["id"],
        "{{ITEMS}}": L["items"],
        "{{METHODS}}": L["methods"],
        "{{PAGES}}": L["pages"],
        "{{ASSETS}}": L["assets"],
        "{{TEMPLATES}}": L["templates"],
        "{{ITEM_COUNT}}": str(len(all_q)),
        "{{PAGE_COUNT}}": str(len(pages)),
    }
    pages_dir = Path(__file__).resolve().parent.parent / "assets" / "pages"
    if pages_dir.is_dir():
        for f in sorted(pages_dir.glob("*.md")):
            dest = (VAULT / L["templates"] if f.stem in ("条目模板", "字段说明") else VAULT / L["overview"]) / f.name
            if dest.exists():              # 不覆盖已存在的手写页
                continue
            text = f.read_text(encoding="utf-8")
            for k, v in subs.items():
                text = text.replace(k, v)
            dest.write_text(text, encoding="utf-8")

    # ---------- 恢复手写页 ----------
    if stash:
        for f in stash.glob("*.md"):
            sub, name = f.name.split("__", 1)
            dest = VAULT / sub.replace("_", " ") / name
            if not dest.exists():
                shutil.copy2(f, dest)
        shutil.rmtree(stash, ignore_errors=True)
        print("手写页已恢复")

    print("\n完成")
    print("章节分布:", dict(ch_stat))
    print("母方法:", dict(sorted(mcount.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()

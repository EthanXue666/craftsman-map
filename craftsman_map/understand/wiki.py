"""
UNDERSTAND 理解层 —— 把代码翻译成人话
=====================================
craftsman-map 本身零 LLM 依赖。描述由"调用方"(任意 Agent/大模型)
用自己的模型生成，本模块只负责三件事：

  1. 规则生成 (rule-based)  —— 零成本、即时、从结构直接产出基础描述
  2. 原料包 (describe)      —— 打包功能块结构给调用方 LLM 生成高质量描述
  3. 缓存 + 块级指纹        —— 描述按功能块绑指纹，代码变了只失效那一块

核心设计（主人 2026-07-20 拍板）：
- 描述不是"生成一次永久复用"。每个功能块独立绑一个指纹
  (该块所有节点 id + signature 的哈希)。块内代码变了 → 该块描述失效，
  自动降级到规则版并标 stale；没变的块直接用缓存，不浪费 token。
- 谁调用地图，谁就是大模型 → 让调用方生成描述，不管它用什么模型。
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict

from ..graph.store import CodeGraph
from ..graph.model import NodeKind, EdgeKind


WIKI_FILE = "wiki.json"


# ============ 块级指纹 ============

def cluster_fingerprint(g: CodeGraph, cid: int) -> str:
    """计算单个功能块的指纹。
    指纹 = 该块所有节点的 (id + signature + docstring) 排序后哈希。
    代码结构/签名/摘要任一变化 → 指纹变 → 描述失效。"""
    parts = []
    for n in g.nodes.values():
        if n.cluster == cid:
            parts.append(f"{n.id}|{n.signature}|{n.docstring}")
    parts.sort()
    raw = "\n".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def all_cluster_fingerprints(g: CodeGraph) -> dict[str, str]:
    """所有功能块的指纹 {cluster_id_str: fingerprint}。"""
    cids = {n.cluster for n in g.nodes.values() if n.cluster >= 0}
    return {str(cid): cluster_fingerprint(g, cid) for cid in cids}


# ============ 缓存读写 ============

def _wiki_path(root: str) -> str:
    return os.path.join(root, ".craftsman-map", WIKI_FILE)


def load_wiki(root: str) -> dict:
    """加载描述缓存。结构:
    {
      "clusters": {
        "0": {"fingerprint": "...", "title": "...", "description": "...",
              "source": "rule|injected", "updated_at": "..."},
        ...
      },
      "project": {"description": "...", "source": "...", "updated_at": "..."}
    }"""
    path = _wiki_path(root)
    if not os.path.exists(path):
        return {"clusters": {}, "project": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"clusters": {}, "project": {}}


def save_wiki(root: str, wiki: dict) -> str:
    d = os.path.join(root, ".craftsman-map")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, WIKI_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(wiki, f, ensure_ascii=False, indent=1)
    return path


# ============ 规则生成 (零成本基础描述) ============

def _cluster_role(g: CodeGraph, cid: int, members: list) -> str:
    """启发式判断功能块的角色标签。"""
    names = " ".join(m.qualified_name.lower() for m in members)
    paths = " ".join(m.path.lower() for m in members if m.path)
    blob = names + " " + paths
    if any(k in blob for k in ("test", "spec", "benchmark", "conftest")):
        return "测试"
    if any(k in blob for k in ("cli", "main", "entry", "__main__", "argparse")):
        return "入口"
    if any(k in blob for k in ("util", "helper", "common", "tool", "shared")):
        return "工具"
    if any(k in blob for k in ("config", "setting", "loader")):
        return "配置"
    if any(k in blob for k in ("model", "schema", "entity", "dataclass")):
        return "数据模型"
    if any(k in blob for k in ("parser", "lexer", "analyz", "scan")):
        return "解析"
    return "核心"


def rule_describe_cluster(g: CodeGraph, cid: int, members: list) -> dict:
    """从结构直接生成一个功能块的规则描述 (无 LLM)。"""
    files = sorted({m.path for m in members if m.path})
    classes = [m for m in members if m.kind == NodeKind.CLASS]
    funcs = [m for m in members if m.kind == NodeKind.FUNCTION]
    # 代表性符号: 有 docstring 的类优先
    reps = sorted(members,
                  key=lambda m: (m.kind != NodeKind.CLASS, not m.docstring,
                                 -m.confidence))
    role = _cluster_role(g, cid, members)
    top_names = [r.qualified_name for r in reps[:5]]
    # docstring 拼一个粗略主旨
    docs = [m.docstring for m in reps[:5] if m.docstring]
    gist = docs[0] if docs else ""
    title_file = os.path.basename(files[0]) if files else f'cluster{cid}'
    title = f"{role}块 · {title_file}"
    desc_lines = [
        f"角色: {role}",
        f"规模: {len(files)} 文件 · {len(classes)} 类 · {len(funcs)} 函数/方法",
        f"关键符号: {', '.join(top_names) if top_names else '(无)'}",
    ]
    if gist:
        desc_lines.append(f"主旨(取自docstring): {gist}")
    return {
        "title": title,
        "role": role,
        "description": "\n".join(desc_lines),
        "source": "rule",
        "key_symbols": top_names,
        "files": files[:15],
    }


# ============ 原料包 (给调用方 LLM 生成高质量描述) ============

def describe_material(g: CodeGraph, cid: int, members: list) -> dict:
    """打包一个功能块的结构原料，供调用方 LLM 阅读后生成描述。
    只给结构 + 签名 + docstring，不倒源码全文 —— 省 token。"""
    files = sorted({m.path for m in members if m.path})
    symbols = []
    reps = sorted(members,
                  key=lambda m: (m.kind != NodeKind.CLASS, not m.docstring,
                                 -m.confidence))
    for m in reps[:25]:
        symbols.append({
            "id": m.id, "name": m.qualified_name, "kind": m.kind.value,
            "signature": m.signature, "docstring": m.docstring,
        })
    # 该块对外的依赖 (指向块外的边)
    member_ids = {m.id for m in members}
    ext_deps = set()
    for m in members:
        for e in g.out_edges(m.id):
            if e.dst not in member_ids and e.dst in g.nodes:
                tgt = g.nodes[e.dst]
                if tgt.cluster != cid:
                    ext_deps.add(tgt.qualified_name)
    return {
        "cluster": cid,
        "file_count": len(files),
        "files": files[:20],
        "symbols": symbols,
        "external_deps": sorted(ext_deps)[:15],
    }


def build_describe_prompt(material: dict) -> str:
    """把原料包渲染成给 LLM 的提示词 (调用方直接喂给自己的模型)。"""
    lines = [
        "你是代码库分析助手。下面是一个代码功能块的结构信息(不含源码全文)。",
        "请用 2-4 句中文，说清楚这个功能块【是干什么的、核心职责、改动它的风险】。",
        "只描述事实，不要编造未给出的细节。",
        "",
        f"功能块 #{material['cluster']} · {material['file_count']} 个文件",
        f"文件: {', '.join(material['files'])}",
        "",
        "关键符号:",
    ]
    for s in material["symbols"]:
        doc = f" — {s['docstring']}" if s["docstring"] else ""
        lines.append(f"  [{s['kind']}] {s['name']}{s['signature'] and ' '+s['signature'] or ''}{doc}")
    if material["external_deps"]:
        lines.append("")
        lines.append(f"对外依赖: {', '.join(material['external_deps'])}")
    lines.append("")
    lines.append("请直接输出描述文本，不要加标题或前缀。")
    return "\n".join(lines)


# ============ 主流程: wiki (读缓存 + 检测失效 + 规则兜底) ============

def _members_by_cluster(g: CodeGraph) -> dict[int, list]:
    groups: dict[int, list] = defaultdict(list)
    for n in g.nodes.values():
        if n.cluster >= 0:
            groups[n.cluster].append(n)
    return groups


def _tag_same_file_titles(clusters: dict) -> None:
    """同文件出现多个块时，给 title 加序号 (N/M) 区分，原地修改。
    例：'核心块 · sessions.py' → '核心块 · sessions.py (1/2)' / '(2/2)'
    """
    from collections import defaultdict
    import re
    # 收集每个文件名对应的 cluster key 列表
    file_to_keys: dict = defaultdict(list)
    for key, c in clusters.items():
        title = c.get("title", "")
        # title 格式: "<角色>块 · <filename>" 或注入后可能是自定义
        m = re.search(r'·\s+(.+)$', title)
        fname = m.group(1).strip() if m else title
        file_to_keys[fname].append(key)
    # 对出现超过 1 次的文件名加序号
    for fname, keys in file_to_keys.items():
        if len(keys) <= 1:
            continue
        for idx, key in enumerate(sorted(keys, key=lambda k: int(k) if k.isdigit() else k), 1):
            c = clusters[key]
            title = c.get("title", "")
            # 避免重复打标
            if not re.search(r'\(\d+/\d+\)$', title):
                c["title"] = f"{title} ({idx}/{len(keys)})"


def build_wiki(root: str, g: CodeGraph, refresh_rules: bool = True) -> dict:
    """核心流程:
    1. 算每个块当前指纹
    2. 对比缓存: 指纹一致 → 复用缓存描述; 不一致/无缓存 → 规则生成 + 标 stale
    3. 返回完整 wiki 视图 + 哪些块需要调用方补 LLM 描述
    """
    cache = load_wiki(root)
    cached_clusters = cache.get("clusters", {})
    groups = _members_by_cluster(g)
    cur_fps = all_cluster_fingerprints(g)

    out_clusters: dict[str, dict] = {}
    # 两个语义严格分开, 不再混为一谈:
    #   needs_description = source=rule (从未注入过高质量描述, 调用方该生成)
    #   stale_clusters    = 曾注入 injected 描述, 但代码变更导致指纹失效 (真 stale, 需重生成)
    needs_description: list[int] = []
    stale_clusters: list[int] = []

    for cid, members in groups.items():
        key = str(cid)
        cur_fp = cur_fps.get(key, "")
        prev = cached_clusters.get(key)
        if prev and prev.get("fingerprint") == cur_fp and prev.get("source") == "injected":
            # 指纹一致且是高质量注入描述 → 直接复用
            out_clusters[key] = dict(prev)
        else:
            # 失效或从未生成 → 规则兜底
            rule = rule_describe_cluster(g, cid, members)
            rule["fingerprint"] = cur_fp
            if prev and prev.get("source") == "injected":
                # 曾注入但指纹变了 → 真 stale (代码改了, 描述过期)
                rule["stale_note"] = "代码已变更, 原 LLM 描述已失效, 当前为规则兜底"
                stale_clusters.append(cid)
            out_clusters[key] = rule
            # 只要当前是规则兜底 (source=rule), 就归入"需要注入描述"
            needs_description.append(cid)

    wiki = {
        "clusters": out_clusters,
        "project": cache.get("project", {}),
        "needs_description": sorted(needs_description),
        "stale_clusters": sorted(stale_clusters),
    }
    # 同文件多块：给 title 加序号 "(N/M)" 区分，避免调用方看到重名块
    _tag_same_file_titles(out_clusters)
    if refresh_rules:
        save_wiki(root, wiki)
    return wiki


def inject_description(root: str, g: CodeGraph, cluster_id: int,
                       description: str, title: str = "") -> dict:
    """调用方生成描述后回写。绑定【当前】块指纹 —— 之后代码变了会自动失效。"""
    cache = load_wiki(root)
    cache.setdefault("clusters", {})
    key = str(cluster_id)
    cur_fp = cluster_fingerprint(g, cluster_id)
    from datetime import datetime
    entry = cache["clusters"].get(key, {})
    entry.update({
        "fingerprint": cur_fp,
        "description": description.strip(),
        "source": "injected",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    if title:
        entry["title"] = title
    cache["clusters"][key] = entry
    save_wiki(root, cache)
    return {"cluster": cluster_id, "fingerprint": cur_fp, "status": "injected"}


def inject_project_description(root: str, description: str) -> dict:
    cache = load_wiki(root)
    from datetime import datetime
    cache.setdefault("project", {})
    cache["project"].update({
        "description": description.strip(),
        "source": "injected",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_wiki(root, cache)
    return {"scope": "project", "status": "injected"}

"""
HTML 知识库处理：编码检测、去脚本/样式、语义线性化，并按章节拆成多「页」供三层分块复用。

设计要点：
- 优先解析 <main>/<article>，否则 <body>，避免全站导航噪声（仍会保留部分 footer，可按需收紧）。
- 将 h1–h4 与段落、列表项按文档顺序线性化，标题转为 Markdown 风格前缀，便于向量区分语义。
- 若存在多个二级标题区块，按区块拆成多个 page_number，改善 chunk_id 分散与可解释性。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import Comment
from langchain_core.documents import Document


def _read_html_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8")
    head = raw[:8192].decode("utf-8", errors="ignore")
    m = re.search(r'charset\s*=\s*["\']?([^"\'>\s]+)', head, re.I)
    if m:
        enc = m.group(1).strip().lower()
        if enc == "utf8":
            enc = "utf-8"
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")


def _strip_noise(soup: BeautifulSoup) -> None:
    for tag in soup(["script", "style", "noscript", "template", "iframe", "svg"]):
        tag.decompose()
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()


def _pick_root(soup: BeautifulSoup) -> Any:
    return soup.find("main") or soup.find("article") or soup.body or soup


def _br_to_newlines(root: Any) -> None:
    """将 <br> 换行，便于后续 get_text 保留行距（原地修改树）。"""
    for br in root.find_all("br"):
        br.replace_with("\n")


def _linearize_blocks(root: Any) -> str:
    """
    按文档顺序抽取块级文本，标题带 Markdown 前缀。

    大量页面把正文放在 div/span 里而不使用 <p>，仅 find p/h 会只剩标题。
    因此在「结构化结果明显短于整页纯文本」时回退为 get_text，避免丢正文。
    """
    _br_to_newlines(root)

    block_tags = [
        "h1",
        "h2",
        "h3",
        "h4",
        "p",
        "li",
        "blockquote",
        "pre",
        "td",
        "th",
        "dd",
        "dt",
        "figcaption",
    ]
    parts: list[str] = []
    for el in root.find_all(block_tags, limit=8000):
        if el.find_parent(["script", "style", "noscript"]):
            continue
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        name = el.name
        if name == "h1":
            parts.append(f"\n# {text}\n")
        elif name == "h2":
            parts.append(f"\n## {text}\n")
        elif name == "h3":
            parts.append(f"\n### {text}\n")
        elif name == "h4":
            parts.append(f"\n#### {text}\n")
        else:
            parts.append(text)

    structured = "\n".join(parts).strip()
    plain = root.get_text("\n", strip=True)

    if not plain:
        return structured
    if not structured:
        return plain

    # 结构化远短于整页文本：典型为正文全在 div/span 或未用 p，只扫到了标题
    if len(structured) < max(120, int(0.22 * len(plain))):
        return plain

    return structured


def _doc_title(soup: BeautifulSoup) -> str:
    t = soup.find("title")
    if t and t.string:
        return t.string.strip()
    return ""


def _split_into_sections(linear: str, page_title: str) -> list[dict[str, Any]]:
    """
    将线性化正文拆成多个 section。
    优先按二级标题 `## ` 切分；否则整篇一页。
    """
    text = linear.strip()
    if not text:
        return []

    # 以行首 ## 切分（与 _linearize_blocks 输出一致）
    pieces = re.split(r"(?m)^(?=## .+$)", text)
    pieces = [p.strip() for p in pieces if p.strip()]
    if len(pieces) <= 1:
        head = f"[{page_title}]\n\n" if page_title else ""
        return [{"page": 1, "title": page_title or "", "text": head + text}]

    sections: list[dict[str, Any]] = []
    for i, block in enumerate(pieces, start=1):
        first_line = block.split("\n", 1)[0].strip()
        sec_title = ""
        if first_line.startswith("## "):
            sec_title = first_line[3:].strip()
        elif i == 1 and first_line.startswith("# ") and not first_line.startswith("##"):
            sec_title = first_line[2:].strip()
        prefix = f"[章节: {sec_title}]\n\n" if sec_title else ""
        sections.append({"page": i, "title": sec_title, "text": prefix + block})
    return sections


def parse_html_file_to_sections(file_path: str | Path) -> list[dict[str, Any]]:
    path = Path(file_path)
    html = _read_html_text(path)
    soup = BeautifulSoup(html, "html.parser")
    _strip_noise(soup)
    root = _pick_root(soup)
    page_title = _doc_title(soup)
    linear = _linearize_blocks(root)
    if page_title and linear and not linear.lstrip().startswith("#"):
        linear = f"# {page_title}\n\n{linear}"
    return _split_into_sections(linear, page_title)


def load_html_for_document_loader(file_path: str, filename: str) -> list[Document]:
    """供 DocumentLoader 使用：与 PDF 一致返回 LangChain Document 列表，metadata.page 对应章节序号。"""
    sections = parse_html_file_to_sections(file_path)
    docs: list[Document] = []
    for sec in sections:
        docs.append(
            Document(
                page_content=sec["text"],
                metadata={
                    "page": sec["page"],
                    "source": filename,
                    "section_title": sec.get("title") or "",
                },
            )
        )
    return docs

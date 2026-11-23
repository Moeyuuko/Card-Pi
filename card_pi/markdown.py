"""Lay out Markdown text for the framebuffer without generating HTML."""

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode


PARSER = MarkdownIt("commonmark", {"html": False}).enable("table")
INLINE_STYLES = {"em": "em", "strong": "strong", "link": "link"}


def inline_runs(nodes, style):
    for node in nodes:
        if node.type in {"softbreak", "hardbreak"}:
            yield "\n", style
        elif node.type == "code_inline":
            yield node.content, "code"
        elif node.children:
            yield from inline_runs(node.children, INLINE_STYLES.get(node.type, style))
        elif node.content:
            yield node.content, style


def markdown_blocks(nodes, indent="", marker=""):
    for node in nodes:
        kind = node.type
        prefix = [(indent + marker, "marker")] if indent or marker else []
        if kind in {"paragraph", "heading"}:
            style = "heading" if kind == "heading" else "body"
            yield prefix + list(inline_runs(node.children, style))
        elif kind in {"fence", "code_block"}:
            for line in node.content.splitlines() or [""]:
                yield prefix + [(line, "code_block")]
                prefix = [(indent, "marker")] if indent else []
        elif kind in {"bullet_list", "ordered_list"}:
            start = int(node.attrs.get("start", 1))
            for index, item in enumerate(node.children, start):
                bullet = f"{index}. " if kind == "ordered_list" else "• "
                yield from markdown_blocks(item.children, indent + " " * len(marker), bullet)
        elif kind == "blockquote":
            yield from markdown_blocks(node.children, indent + " " * len(marker) + "│ ")
        elif kind == "table":
            for section in node.children:
                for row in section.children:
                    cells = prefix.copy()
                    for index, cell in enumerate(row.children):
                        if index:
                            cells.append((" │ ", "marker"))
                        cells.extend(inline_runs(cell.children, "strong" if cell.type == "th" else "body"))
                    yield cells
        elif kind == "hr":
            yield prefix + [("────────", "marker")]
        indent += " " * len(marker)
        marker = ""


def message_blocks(text, render):
    if render:
        root = SyntaxTreeNode(PARSER.parse(text))
        yield from markdown_blocks(root.children)
    else:
        for line in text.splitlines() or [""]:
            yield [(line, "body")]


def wrap_runs(runs, width, font_for):
    line = []
    remaining = width
    for text, style in runs:
        for index, part in enumerate(text.expandtabs(4).split("\n")):
            if index:
                yield line
                line = []
                remaining = width
            font = font_for(style, part)
            if not part and style == "code_block":
                line.append(("", style, font, 0))
            start = 0
            while start < len(part):
                end = min(start + 300, len(part))
                if font.getlength(part[start:end]) > remaining:
                    low, high = start, end
                    while low < high:
                        middle = (low + high + 1) // 2
                        if font.getlength(part[start:middle]) <= remaining:
                            low = middle
                        else:
                            high = middle - 1
                    end = low
                if end < len(part) and style not in {"code", "code_block"} and not part[end].isspace():
                    boundary = part.rfind(" ", start, end)
                    if boundary > start and part[start:boundary].strip():
                        end = boundary
                    elif line and boundary == -1 and line[-1][0][-1:].isspace():
                        end = start
                if end == start and not line:
                    # A single glyph may be wider than the entire configured line.
                    end += 1
                if end > start:
                    fragment = part[start:end]
                    measured = font.getlength(fragment)
                    line.append((fragment, style, font, measured))
                    remaining -= measured
                if end < len(part):
                    yield line
                    line = []
                    remaining = width
                    if style not in {"code", "code_block"} and end > start:
                        while end < len(part) and part[end] == " ":
                            end += 1
                start = end
    yield line

"""Position-aware XML parsing built on expat.

The warehouse must never map AIDX fields by string replacement.  Every mapped
element and attribute is read through this parser and carries a source
location (line/column + 1-based XPath), so fact provenance can point at the
exact original XML position.

The parser also records *unknown* elements/attributes so the AIDX profile
layer can report unsupported constructs instead of silently dropping them.
"""
from __future__ import annotations

import xml.parsers.expat as expat
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class XmlNode:
    tag: str                      # local name
    namespace: str                # namespace URI ("" if none)
    qname: str                    # original qualified name
    attrs: dict[str, str]
    attr_ns: dict[str, str]      # attr local name -> namespace URI
    text: str = ""
    line: int = 0
    column: int = 0
    attr_loc: dict[str, tuple[int, int]] = field(default_factory=dict)
    children: list["XmlNode"] = field(default_factory=list)
    parent: Optional["XmlNode"] = None
    index: int = 1                # 1-based position among same-named siblings

    def child(self, tag: str) -> Optional["XmlNode"]:
        for c in self.children:
            if c.tag == tag:
                return c
        return None

    def children_named(self, tag: str) -> list["XmlNode"]:
        return [c for c in self.children if c.tag == tag]

    def xpath(self) -> str:
        parts: list[str] = []
        node: Optional[XmlNode] = self
        while node is not None and node.parent is not None:
            parts.append(f"/{node.tag}[{node.index}]")
            node = node.parent
        if node is not None:  # root
            parts.append(f"/{node.tag}[{node.index}]")
        return "".join(reversed(parts))

    def location(self) -> str:
        return f"{self.xpath()} @line {self.line}:{self.column}"


@dataclass
class ParseIssue:
    kind: str          # "unsupported_element" | "unsupported_attribute" | "xml_error"
    name: str
    line: int
    column: int
    xpath: str
    detail: str = ""


class XmlParseError(Exception):
    def __init__(self, message: str, line: int = 0, column: int = 0):
        super().__init__(message)
        self.line = line
        self.column = column


def parse_xml(data: bytes | str, known_namespaces: set[str] | None = None
              ) -> tuple[XmlNode, list[ParseIssue]]:
    """Parse XML and return (root, issues).

    ``known_namespaces`` is used only to flag namespace-level profile
    violations; element-level supported/unsupported classification happens in
    the AIDX profile module.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    p = expat.ParserCreate(namespace_separator="\x1f")
    root: list[XmlNode] = []
    stack: list[XmlNode] = []
    issues: list[ParseIssue] = []
    sibling_counts: dict[str, int] = {}

    def start(name: str, attrs: dict[str, str]) -> None:
        if "\x1f" in name:
            ns, local = name.split("\x1f", 1)
        else:
            ns, local = "", name
        attr_map: dict[str, str] = {}
        attr_ns: dict[str, str] = {}
        attr_loc: dict[str, tuple[int, int]] = {}
        for raw_k, v in attrs.items():
            if "\x1f" in raw_k:
                a_ns, a_local = raw_k.split("\x1f", 1)
            else:
                a_ns, a_local = "", raw_k
            attr_map[a_local] = v
            attr_ns[a_local] = a_ns
            attr_loc[a_local] = (p.CurrentLineNumber, p.CurrentColumnNumber)
        node = XmlNode(
            tag=local, namespace=ns, qname=name.replace("\x1f", ":"),
            attrs=attr_map, attr_ns=attr_ns, attr_loc=attr_loc,
            line=p.CurrentLineNumber, column=p.CurrentColumnNumber,
        )
        if known_namespaces and ns and ns not in known_namespaces:
            issues.append(ParseIssue(
                "unknown_namespace", local, node.line, node.column,
                node.qname, f"namespace {ns!r} not declared by profile",
            ))
        if stack:
            parent = stack[-1]
            key = f"{id(parent)}:{local}"
            sibling_counts[key] = sibling_counts.get(key, 0) + 1
            node.index = sibling_counts[key]
            node.parent = parent
            parent.children.append(node)
        else:
            node.index = 1
            root.append(node)
        stack.append(node)

    def end(name: str) -> None:
        stack.pop()

    def chars(text: str) -> None:
        if stack:
            stack[-1].text += text

    p.StartElementHandler = start
    p.EndElementHandler = end
    p.CharacterDataHandler = chars
    try:
        p.Parse(data, True)
    except expat.ExpatError as exc:
        raise XmlParseError(str(exc), exc.lineno, exc.offset) from exc
    if not root:
        raise XmlParseError("document has no root element")
    return root[0], issues

"""Keep compact, source-faithful README material for local candidate review."""

import re
from html import unescape


_FENCE_RE = re.compile(r'^\s*(`{3,}|~{3,})(.*)$')


def _next_fence(line: str, active: str | None) -> str | None:
    match = _FENCE_RE.match(line)
    if not match:
        return active
    marker, suffix = match.groups()
    if active is None:
        return marker
    if marker[0] == active[0] and len(marker) >= len(active) and not suffix.strip():
        return None
    return active


def clean_readme(text: str, max_chars: int = 12000) -> tuple[str, bool]:
    """Return useful Markdown text and whether the retained text hit the length cap."""
    marker = '[内容已截断]'
    if not isinstance(text, str):
        raise ValueError('README 正文必须为字符串')
    if type(max_chars) is not int or max_chars <= len(marker) + 1:
        raise ValueError('README 正文上限过小')

    source = unescape(text.replace('\r\n', '\n').replace('\r', '\n').replace('\x00', ''))
    source = re.sub(r'<!--.*?-->', '', source, flags=re.DOTALL)
    source = re.sub(r'<(?P<tag>script|style|picture|svg)\b[^>]*>.*?</(?P=tag)>', '', source,
                    flags=re.IGNORECASE | re.DOTALL)
    source = re.sub(
        r'<h([1-6])\b[^>]*>(.*?)</h\1>',
        lambda match: '\n' + '#' * int(match.group(1)) + ' ' + re.sub(r'<[^>]+>', '', match.group(2)).strip() + '\n',
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )

    lines: list[str] = []
    fence: str | None = None
    for raw_line in source.split('\n'):
        line = raw_line.rstrip()
        if _FENCE_RE.match(line):
            lines.append(line.strip())
            fence = _next_fence(line, fence)
            continue
        if fence is not None:
            lines.append(line)
            continue
        if re.match(r'^\s*#{1,6}\s*(?:table of contents|contents|目录|索引)\s*#*\s*$', line,
                    flags=re.IGNORECASE):
            continue
        line = re.sub(r'!\[[^\]]*\]\([^)]*\)|!\[[^\]]*\]\[[^\]]*\]', '', line)
        line = re.sub(r'<img\b[^>]*>', '', line, flags=re.IGNORECASE)
        line = re.sub(r'\[\s*\]\([^)]*\)|<a\b[^>]*>\s*</a>', '', line, flags=re.IGNORECASE)
        if re.search(r'\[[^\]]+\]\([^)]*\)|<a\b[^>]*>.*?</a>', line, flags=re.IGNORECASE):
            without_links = re.sub(r'\[[^\]]+\]\([^)]*\)|<a\b[^>]*>.*?</a>', '', line,
                                   flags=re.IGNORECASE)
            if not re.sub(r'[\s|/·•\-*+0-9.()]', '', without_links):
                continue
        line = re.sub(r'</?(?:p|div|center|span|strong|em|br|a)\b[^>]*>', '', line,
                      flags=re.IGNORECASE).strip()
        if line or (lines and lines[-1]):
            lines.append(line)
    if fence is not None:
        lines.append(fence)

    sections: list[tuple[int, str, list[str]]] = []
    heading = ''
    level = 0
    current: list[str] = []
    fence = None
    for line in lines:
        match = (re.match(r'^\s*(#{1,6})\s+(.+?)\s*#*\s*$', line)
                 if fence is None else None)
        if match:
            if current:
                sections.append((level, heading, current))
            level = len(match.group(1))
            heading = match.group(2).strip().casefold()
            current = [line]
        else:
            current.append(line)
        fence = _next_fence(line, fence)
    if current:
        sections.append((level, heading, current))

    focus_words = (
        '介绍', '简介', '概述', '用途', '核心功能', '功能', '特性', '技术栈', '架构',
        '依赖', '环境', '前提', '要求', '快速开始', '入门', '安装', '使用', '运行', '启动',
    )
    focus_english = re.compile(
        r'\b(?:about|overview|introduction|features?|capabilities|tech(?:nology)? stack|'
        r'architecture|requirements?|prerequisites?|quick\s*start|getting started|'
        r'installation|install|usage|setup|running|run)\b'
    )
    chosen: set[int] = set()
    found_focus = False
    if sections:
        chosen.add(0)
    for index, (section_level, section_heading, _) in enumerate(sections):
        if not (any(word in section_heading for word in focus_words)
                or focus_english.search(section_heading)):
            continue
        found_focus = True
        chosen.add(index)
        for child in range(index + 1, len(sections)):
            if sections[child][0] <= section_level:
                break
            chosen.add(child)
    if not found_focus:
        chosen = set(range(len(sections)))
    retained = '\n'.join(
        '\n'.join(sections[index][2]).strip()
        for index in sorted(chosen)
        if '\n'.join(sections[index][2]).strip()
    ).strip()
    if len(retained) <= max_chars:
        return retained, False

    allowance = max_chars - len(marker) - 1
    kept_lines: list[str] = []
    fence = None
    for line in retained.split('\n'):
        next_fence = _next_fence(line, fence)
        candidate = '\n'.join((*kept_lines, line))
        closing_length = len(next_fence) + 1 if next_fence is not None else 0
        if len(candidate) + closing_length > allowance:
            break
        kept_lines.append(line)
        fence = next_fence
    if not kept_lines:
        first_line = retained.split('\n', 1)[0]
        if _FENCE_RE.match(first_line):
            return marker, True
        return retained[:allowance].rstrip() + '\n' + marker, True
    prefix = '\n'.join(kept_lines).rstrip()
    if fence is not None:
        prefix += '\n' + fence
    return prefix + '\n' + marker, True

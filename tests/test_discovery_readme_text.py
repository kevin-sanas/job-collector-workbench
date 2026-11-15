import unittest

from app.discovery.readme_text import clean_readme


class ReadmeTextTests(unittest.TestCase):
    def test_preserves_project_facts_and_commands_without_badges_or_navigation(self):
        original = '''# SampleKit ![CI](https://example.test/badge.svg)
[![License](https://example.test/license.svg)](https://example.test/license)
<img src="https://example.test/banner.png" alt="banner">

SampleKit combines local notes into a searchable index.

## Table of Contents
- [Features](#features)
- [Quick Start](#quick-start)
[English](README.en.md) | [中文](README.zh.md)

## Features
- Search notes by keyword.
- Export search results.

## Tech Stack
Python and SQLite.

## Requirements
Python 3.13 is required.

## Quick Start
```bash
python -m samplekit init
python -m samplekit search hello
```

## Changelog
Hundreds of older release notes.
'''

        cleaned, truncated = clean_readme(original)

        self.assertFalse(truncated)
        for expected in (
            'SampleKit combines local notes', 'Search notes by keyword',
            'Python and SQLite', 'Python 3.13 is required',
            'python -m samplekit init',
        ):
            self.assertIn(expected, cleaned)
        for removed in ('badge.svg', 'banner.png', 'Table of Contents',
                        '[Features](#features)', '[English]', 'Changelog'):
            self.assertNotIn(removed, cleaned)

    def test_keeps_plain_readme_without_headings(self):
        cleaned, truncated = clean_readme(
            'This tool indexes files locally.\n\nRun `python app.py` to begin.'
        )

        self.assertFalse(truncated)
        self.assertIn('indexes files locally', cleaned)
        self.assertIn('python app.py', cleaned)

    def test_drops_unrelated_sections_when_first_heading_is_focused(self):
        cleaned, truncated = clean_readme(
            '## Features\nIndexes files.\n\n## Changelog\nOld release notes.\n'
        )

        self.assertFalse(truncated)
        self.assertIn('Indexes files', cleaned)
        self.assertNotIn('Old release notes', cleaned)

    def test_chinese_headings_and_html_images(self):
        original = '''<h1>示例</h1>
这是一个本地索引程序。
<picture><img src="banner.svg"></picture>
## 核心功能
按关键词查询文件。
## 运行前提
需要 Python。
## 快速开始
运行 `python main.py`。
'''

        cleaned, truncated = clean_readme(original)

        self.assertFalse(truncated)
        self.assertIn('这是一个本地索引程序', cleaned)
        self.assertIn('按关键词查询文件', cleaned)
        self.assertIn('需要 Python', cleaned)
        self.assertIn('python main.py', cleaned)
        self.assertNotIn('banner.svg', cleaned)

    def test_marks_length_truncation_within_limit(self):
        original = '# Example\n' + ('A long factual introduction. ' * 100)

        cleaned, truncated = clean_readme(original, max_chars=180)

        self.assertTrue(truncated)
        self.assertLessEqual(len(cleaned), 180)
        self.assertTrue(cleaned.endswith('[内容已截断]'))
        self.assertNotIn('[内容已截断]', clean_readme('# Short\nShort.', max_chars=180)[0])

    def test_heading_inside_quick_start_code_keeps_commands_and_closing_fence(self):
        original = '''# Example
Useful introduction.
## Quick Start
```bash
# Install dependencies
python -m pip install -r requirements.txt
python main.py
```
## Changelog
Old release notes.
'''

        cleaned, truncated = clean_readme(original)

        self.assertFalse(truncated)
        self.assertIn('python -m pip install -r requirements.txt', cleaned)
        self.assertIn('python main.py', cleaned)
        self.assertNotIn('Old release notes', cleaned)
        self.assertEqual(cleaned.count('```'), 2)

    def test_unrelated_code_heading_does_not_leak_or_orphan_fence(self):
        original = '''# Example
Useful introduction.
## Changelog
```bash
# Quick Start
obsolete-command
```
## Features
Searchable notes.
## Quick Start
```bash
python main.py
```
'''

        cleaned, truncated = clean_readme(original)

        self.assertFalse(truncated)
        self.assertNotIn('obsolete-command', cleaned)
        self.assertIn('python main.py', cleaned)
        self.assertEqual(cleaned.count('```'), 2)

    def test_truncation_keeps_complete_command_and_balances_code_fence(self):
        original = '''# Example
Useful introduction.
## Quick Start
```bash
python -m pip install -r requirements.txt
python main.py
''' + ('python run-long-command-with-extra-arguments\n' * 20) + '```\n'

        cleaned, truncated = clean_readme(original, max_chars=135)

        self.assertTrue(truncated)
        self.assertLessEqual(len(cleaned), 135)
        self.assertIn('python -m pip install -r requirements.txt', cleaned)
        self.assertIn('[内容已截断]', cleaned)
        self.assertEqual(cleaned.count('```'), 2)

    def test_malformed_unclosed_source_fence_is_closed_in_excerpt(self):
        cleaned, truncated = clean_readme('## Quick Start\n```bash\npython app.py\n')

        self.assertFalse(truncated)
        self.assertIn('python app.py', cleaned)
        self.assertEqual(cleaned.count('```'), 2)


if __name__ == '__main__':
    unittest.main()

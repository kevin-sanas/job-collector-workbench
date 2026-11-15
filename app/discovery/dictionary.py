import json
from dataclasses import dataclass
from pathlib import Path

from app.discovery.models import TechDictionary, normalize_alias


@dataclass(frozen=True)
class CompiledAlias:
    alias: str
    normalized_alias: str
    tech_key: str
    match_mode: str
    review_required: bool


def build_alias_index(dictionary: TechDictionary) -> tuple[CompiledAlias, ...]:
    aliases: dict[str, CompiledAlias] = {}
    for term in dictionary.terms:
        if not term.enabled:
            continue
        for alias in term.aliases:
            normalized = normalize_alias(alias)
            existing = aliases.get(normalized)
            if existing and existing.tech_key != term.tech_key:
                raise ValueError(
                    f'启用词典别名 {alias!r} 同时属于 {existing.tech_key} 和 {term.tech_key}'
                )
            aliases[normalized] = CompiledAlias(
                alias=alias,
                normalized_alias=normalized,
                tech_key=term.tech_key,
                match_mode=term.match_mode,
                review_required=term.match_mode == 'review',
            )
    return tuple(sorted(aliases.values(), key=lambda item: (item.normalized_alias, item.tech_key)))


def load_dictionary(path: Path) -> TechDictionary:
    try:
        values = json.loads(Path(path).read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'技术词典 JSON 无效：{exc.msg}') from None
    dictionary = TechDictionary.from_mapping(values)
    build_alias_index(dictionary)
    return dictionary


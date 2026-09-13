from pathlib import Path
from typing import Any, Dict, Optional
import json


class DictLookup:
    def __init__(self, data_dir: Path, custom_aliases: Optional[Dict[str, str]] = None):
        self.dict_path = data_dir / "dict.json"
        self.names_path = data_dir / "names.json"
        self._main_dict: Optional[Dict[str, Dict[str, Any]]] = None
        self._name_index: Dict[str, str] = {}
        self._lowercase_index: Dict[str, str] = {}
        self._character_by_en: Dict[str, str] = {}
        self._character_names_cache: Optional[list] = None
        self.custom_aliases: Dict[str, str] = {}
        self._custom_aliases_lower: Dict[str, str] = {}
        if custom_aliases:
            self.set_custom_aliases(custom_aliases)

    def set_custom_aliases(self, aliases: Dict[str, str]) -> None:
        """设置用户自定义别名映射（俗称/错别字 → 官方名/英文名/条目ID）。"""
        self.custom_aliases = dict(aliases or {})
        self._custom_aliases_lower = {
            k.lower(): v for k, v in self.custom_aliases.items() if isinstance(k, str)
        }
        self._character_names_cache = None  # 别名更新后失效角色名缓存

    def _load(self) -> Dict[str, Dict[str, Any]]:
        """加载精简字典与名字索引并返回主字典（幂等：首次调用解析字典 JSON）。

        返回主字典而非在此后访问 self._main_dict，让类型收窄为已加载形态，
        调用方无需再做 Optional 判定。
        """
        if self._main_dict is None:
            if not self.dict_path.is_file() or not self.names_path.is_file():
                raise FileNotFoundError("dict files not found")
            with self.dict_path.open("r", encoding="utf-8") as f:
                main: Dict[str, Dict[str, Any]] = json.load(f)
            with self.names_path.open("r", encoding="utf-8") as f:
                self._name_index = json.load(f)
            # 小写索引预建：lookup_term 的大小写回落从 O(n) 扫描降为 O(1) 哈希查找
            self._lowercase_index = {name.lower(): key for name, key in self._name_index.items()}
            self._main_dict = main
            self._character_by_en = self._build_character_index(main)
        loaded = self._main_dict
        assert loaded is not None  # 上面分支保证：字典已就绪
        return loaded

    def _resolve_key(
        self,
        term: str,
        active_aliases: Dict[str, str],
        active_aliases_lower: Dict[str, str],
    ) -> Optional[str]:
        """解析查询词为 dict.json 的 key（支持多层别名映射与大小写回落）。"""
        main = self._load()
        current = term
        visited: set[str] = set()
        # 最多跟踪 5 层别名映射，防止配置出现环路
        for _ in range(5):
            if current in visited:
                break
            visited.add(current)
            # 1. 优先查精确自定义别名
            if current in active_aliases:
                target = active_aliases[current]
                if target in main:
                    return target
                current = target
                continue
            # 2. 自定义别名小写匹配
            curr_lower = current.lower()
            if curr_lower in active_aliases_lower:
                target = active_aliases_lower[curr_lower]
                if target in main:
                    return target
                current = target
                continue
            break

        # 检查是否直接就是 main_dict 的 key
        if current in main:
            return current

        # 查官方名字索引（精确匹配优先，大小写不敏感回落）
        key = self._name_index.get(current)
        if not key:
            key = self._lowercase_index.get(current.lower())
        return key

    def lookup_term(
        self, term: str, custom_aliases: Optional[Dict[str, str]] = None
    ) -> Optional[Dict[str, str]]:
        main = self._load()
        if not term:
            return None
        active_aliases = self.custom_aliases if custom_aliases is None else dict(custom_aliases)
        active_aliases_lower = (
            self._custom_aliases_lower
            if custom_aliases is None
            else {k.lower(): v for k, v in active_aliases.items() if isinstance(k, str)}
        )
        key = self._resolve_key(term, active_aliases, active_aliases_lower)
        if key and key in main:
            entry = main[key]
            # 角色查询兜底：命中非 Character 条目（如 CharacterDes 描述条目、
            # Item 等同名条目）但存在同英文名的 Character 条目时，改路由到
            # Character 条目——否则 query_how 会因 cat≠Character 跳过攻略抓取
            if entry.get("cat") != "Character" and entry.get("en"):
                char_key = self._character_by_en.get(entry["en"])
                if char_key:
                    entry = main[char_key]
                    key = char_key
            return {"id": key, "en": entry.get("en", ""), "cn": entry.get("cn", ""), "cat": entry.get("cat", "")}
        return None

    def get_by_id(self, item_id: str) -> Optional[Dict[str, Any]]:
        return self._load().get(item_id)

    def get_character_index(self) -> Dict[str, str]:
        """返回 en → Character 条目 ID 的索引（加载时构建并缓存，供队伍表构建复用）。"""
        self._load()
        return self._character_by_en

    def get_character_names(self) -> list:
        """所有 Character 类目的 en+cn 名（去重，按长度降序）。

        用于联合查询检测：用户问题中命中 ≥2 个角色名即视为多角色联合查询。
        变体名（如 薇洛（盛夏））会额外纳入剥离括号后的基础名（薇洛），
        用户用基础名提问时也能命中。
        结果首次计算后缓存（_character_names_cache），后续调用直接复用。
        """
        main = self._load()
        if self._character_names_cache is not None:
            return self._character_names_cache
        names: set = set()
        for entry in main.values():
            if entry.get("cat") != "Character":
                continue
            for key in ("en", "cn"):
                name = (entry.get(key) or "").strip()
                if len(name) >= 2:
                    names.add(name)
                # 变体名剥离括号基础名（薇洛（盛夏）→ 薇洛）
                base = name.split("（")[0].strip()
                if len(base) >= 2 and base != name:
                    names.add(base)
        # 纳入自定义别名中指向 Character 的别名（用于联合查询检测）
        for alias in self.custom_aliases:
            alias_clean = alias.strip()
            if len(alias_clean) >= 2:
                res = self.lookup_term(alias_clean)
                if res and res.get("cat") == "Character":
                    names.add(alias_clean)
        self._character_names_cache = sorted(names, key=len, reverse=True)
        return self._character_names_cache

    def _build_character_index(self, main: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
        """构建 en → Character 条目 ID 的索引（同名时取最小 key，稳定优先）。"""
        index: Dict[str, str] = {}
        for key, entry in main.items():
            if entry.get("cat") != "Character":
                continue
            en = entry.get("en", "")
            if not en:
                continue
            if en not in index or key < index[en]:
                index[en] = key
        return index

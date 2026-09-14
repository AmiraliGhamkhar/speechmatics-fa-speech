
from __future__ import annotations
import json, re
from pathlib import Path
from typing import Any

from .text import normalize_text

class MedicalLayer:
    """
    Conservative terminology layer.
    It never uses an LLM and never invents new medical facts.
    It applies only explicit aliases from user-provided or user-observed data.
    """
    def __init__(self, root: Path):
        self.root = root
        self.nursing = self._load("medical_knowledge/nursing_terms.json").get("entries", [])
        self.abbreviations = self._load("medical_knowledge/abbreviations.json")
        self.phrases = self._load("medical_knowledge/nursing_phrases.json")
        self.aliases = self._load("medical_knowledge/observed_asr_aliases.json")

    def _load(self, rel):
        return json.loads((self.root / rel).read_text(encoding="utf-8"))

    def normalize(self, text: str) -> tuple[str, list[dict[str, Any]]]:
        out = normalize_text(text)
        hits = []
        # Longest-first deterministic replacements.
        replacements = []
        for canonical, data in self.aliases.items():
            for form in data.get("common_asr_forms", []) + data.get("spoken_forms", []):
                replacements.append((normalize_text(form), canonical, 0.98, "observed_alias"))
        for key, data in self.abbreviations.items():
            for form in data.get("spoken", []):
                replacements.append((normalize_text(form), key, 0.96, "abbreviation"))
        for p in self.phrases:
            for form in p.get("spoken_forms", []):
                replacements.append((normalize_text(form), p["canonical"], 0.90, "phrase"))
        for r in self.nursing:
            for form in r.get("spoken_forms", []):
                replacements.append((normalize_text(form), r["canonical"], 0.88, "nursing_term"))

        replacements.sort(key=lambda x: len(x[0]), reverse=True)
        for form, canonical, confidence, source in replacements:
            if not form or not canonical:
                continue
            pattern = re.escape(form)
            new, n = re.subn(pattern, canonical, out, flags=re.IGNORECASE)
            if n:
                out = new
                hits.append({"source":source,"input":form,"output":canonical,"confidence":confidence,"count":n})
        return out, hits
